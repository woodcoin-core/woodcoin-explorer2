# apt install postgresql postgresql-client redis redis-server
# ----------------------------------------------
# Use Python 3.3+ because of `decimal` issues:
# https://docs.sqlalchemy.org/en/14/core/type_basics.html#sqlalchemy.types.Numeric
import datetime
import logging
import math
import sys
from decimal import Decimal
from json import JSONEncoder
from logging.handlers import RotatingFileHandler
from flask import Flask, json, send_from_directory
from flask import redirect, request, url_for, render_template
from flask_caching import Cache
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFError, CSRFProtect
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool
from sqlalchemy.sql import desc
from werkzeug.middleware.proxy_fix import ProxyFix
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, Length
import blockchain
from config import coin_name, rpcpassword, rpcport, rpcuser
from config import app_key, csrf_key, database_uri, program_name
from helpers import chain_age, JSONRPC, JSONRPCException
from models import db, Blocks, CoinbaseTXIn, TXs, TXIn, TxOut, Addresses, AddressSummary


class DecimalEncoder(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return f"{obj:0.8f}"
        return JSONEncoder.default(self, obj)


def create_app(the_csrf):
    prep_application = Flask(__name__)
    prep_application.debug = False
    prep_application.json_encoder = DecimalEncoder
    # setup RotatingFileHandler with maxBytes set to 25MB
    rotating_log = RotatingFileHandler('cryptocurrency_explorer.log', maxBytes=25000000, backupCount=6)
    prep_application.logger.addHandler(rotating_log)
    rotating_log.setFormatter(logging.Formatter(fmt='[%(asctime)s] / %(levelname)s in %(module)s: %(message)s'))
    prep_application.logger.setLevel(logging.INFO)
    prep_application.secret_key = app_key
    if coin_name != '' or coin_name is not None:
        # check blockchain/README.md for this
        prep_application.config['COIN_NAME'] = coin_name.capitalize()
    else:
        prep_application.logger.error("coin_name in config.py needs to be set.")
        sys.exit()
    try:
        coin__uniques = getattr(blockchain, prep_application.config['COIN_NAME'])().unique
    # TypeError needs caught in case someone tries non-strings for the coin_name... for whatever reason?
    except(AttributeError, TypeError):
        prep_application.logger.error("coin_name in config.py is not a supported coin.")
        sys.exit()
    prep_application.config['MAX_CONTENT_LENGTH'] = 1024
    # 30 days
    prep_application.config['PERMANENT_SESSION_LIFETIME'] = 2592000
    prep_application.config['PROGRAM_NAME'] = program_name
    # This appears to be an issue -- https://github.com/wtforms/flask-wtf/issues/521
    # prep_application.config['REMEMBER_COOKIE_HTTPONLY'] = True
    #
    # Enable this in production
    # prep_application.config['SESSION_COOKIE_HTTPONLY'] = True
    prep_application.config['SESSION_COOKIE_NAME'] = 'csrf_token'
    prep_application.config['SESSION_COOKIE_SAMESITE'] = 'Strict'
    # Enable this in production
    # prep_application.config['SESSION_COOKIE_SECURE'] = True
    prep_application.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    prep_application.config['SQLALCHEMY_DATABASE_URI'] = database_uri
    prep_application.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'poolclass': NullPool}
    prep_application.config['VERSION'] = 0.8
    prep_application.config['WTF_CSRF_SECRET_KEY'] = csrf_key
    prep_application.jinja_env.trim_blocks = True
    prep_application.jinja_env.lstrip_blocks = True
    prep_application.jinja_env.enable_async = True
    prep_application.wsgi_app = ProxyFix(prep_application.wsgi_app, x_proto=1, x_host=1)
    the_cache = Cache(config={'CACHE_TYPE': 'RedisCache',
                              'CACHE_KEY_PREFIX': 'cce',
                              'CACHE_REDIS_URL': 'redis://localhost:6379/0'})
    the_cache.init_app(prep_application)
    db.init_app(prep_application)
    the_csrf.init_app(prep_application)
    rpcurl = f"http://127.0.0.1:{rpcport}"
    try:
        crypto_currency = JSONRPC(rpcurl, rpcuser, rpcpassword)
    except ValueError:
        prep_application.logger.error("One of these is wrong: rpcuser/rpcpassword/rpcport. Fix this in config.py.")
        sys.exit()
    return prep_application, the_cache, coin__uniques, crypto_currency


csrf = CSRFProtect()
application, cache, coin_uniques, cryptocurrency = create_app(csrf)
application.app_context().push()


@application.template_global()
def format_time(timestamp):
    return datetime.datetime.fromtimestamp(timestamp)


@application.template_global()
def format_size(tx_size):
    return tx_size / 1000.0


@application.template_global()
def format_eight_zeroes(the_item):
    if the_item == 0:
        return '0.00000000'
    else:
        return format(the_item, '.8f')


# When first_run is executing, this needs to happen if we want to also view the explorer
# Not sure if I'm keeping this, or if this is the best way to approach this.
@application.errorhandler(SQLAlchemyError)
def sqlalchemy_error(error):
    db.session.rollback()


@application.errorhandler(CSRFError)
def handle_csrf_error(e):
    return render_template('404.html', error=e.description), 400


@application.errorhandler(400)
def handle_bad_request():
    error = 'bad request'
    return render_template("404.html", error=error), 400


@application.errorhandler(404)
def not_found(e):
    error = f'{request.environ["RAW_URI"]} was not found'
    return render_template("404.html", error=error), 404


@application.errorhandler(413)
def payload_too_large():
    error = f'payload too large'
    return render_template("404.html", error=error), 413


@application.errorhandler(414)
def uri_too_large():
    error = f'URI too large'
    return render_template("404.html", error=error), 414


@application.route('/robots.txt')
# Cached for 30 days
@cache.cached(timeout=2592000)
def robots():
    return send_from_directory(application.static_folder, 'robots.txt')


class SearchForm(FlaskForm):
    search = StringField('Search',
                         validators=[DataRequired(), Length(min=1, max=64)],
                         render_kw={"placeholder": "Search address, blocks, transactions"})
    submit = SubmitField('Submit')


@application.get("/")
@application.post("/")
@cache.memoize(300)
def index():
    form = SearchForm(request.form)
    count = request.args.get('count', default=50, type=int)
    try:
        if 1 <= count <= 500:
            count = count
        else:
            count = 25
    except ValueError:
        count = 1

    latest_block_height = int(db.session.query(Blocks).order_by(desc('height')).first().height)
    hi = request.args.get('hi', default=latest_block_height, type=int)
    try:
        if hi in range(0, latest_block_height + 1):
            hi = hi
        else:
            hi = 0
    except ValueError:
        hi = 0

    front_page_items = db.session.query(Blocks).where(Blocks.height <= hi).order_by(desc('height')).limit(count)
    genesis_timestamp = coin_uniques['genesis']['timestamp']

    if request.method == 'POST':
        address_prefixes = ('address:', 'a:', 'add:')
        block_prefixes = ('block:', 'b:', 'bhash:')
        tx_prefixes = ('transaction:', 't:', 'tx:', 'thash:')
        if form.validate_on_submit():
            if 76 >= len(form.search.data) >= 1:
                if form.search.data.startswith(address_prefixes):
                    if 72 >= len(form.search.data) >= 6:
                        the_address = ''.join(form.search.data.split(':')[1:])
                        address_lookup = db.session.query(AddressSummary).filter(AddressSummary.address.ilike(f"%{the_address}%")).all()
                        if address_lookup:
                            if len(address_lookup) == 1:
                                return redirect(url_for('address', the_address=address_lookup[0].address))
                            else:
                                return render_template('search_results.html',
                                                       searched_addresses=address_lookup,
                                                       searched_blocks=[],
                                                       searched_txs=[])
                        else:
                            # TODO - set a variable for being unable to find specifically addresses
                            return render_template('index.html',
                                                   search_validated=False,
                                                   form=form,
                                                   front_page_blocks=front_page_items,
                                                   format_time=format_time,
                                                   count=count,
                                                   hi=hi,
                                                   latest_block=latest_block_height,
                                                   chain_age=chain_age,
                                                   genesis_time=genesis_timestamp), 200
                    else:
                        return render_template('index.html',
                                               input_too_short=True,
                                               form=form,
                                               front_page_blocks=front_page_items,
                                               format_time=format_time,
                                               count=count,
                                               hi=hi,
                                               latest_block=latest_block_height,
                                               chain_age=chain_age,
                                               genesis_time=genesis_timestamp), 200
                elif form.search.data.startswith(block_prefixes):
                    if 70 >= len(form.search.data) >= 1:
                        the_block = ''.join(form.search.data.split(':')[1:])
                        try:
                            if int(the_block) in range(0, latest_block_height + 1):
                                return redirect(url_for('block', block_hash_or_height=the_block))
                            else:
                                raise ValueError
                        except ValueError:
                            if 70 >= len(form.search.data) >= 6:
                                block_lookup = db.session.query(Blocks).filter(Blocks.hash.like(f"%{the_block}%")).all()
                                if len(block_lookup) == 1:
                                    return redirect(url_for('block', block_hash_or_height=block_lookup[0].hash))
                                elif len(block_lookup) >= 2:
                                    return render_template('search_results.html',
                                                           searched_addresses=[],
                                                           searched_blocks=block_lookup,
                                                           searched_txs=[])
                                else:
                                    # TODO - set a variable for being unable to find specifically blocks
                                    return render_template('index.html',
                                                           search_validated=False,
                                                           form=form,
                                                           front_page_blocks=front_page_items,
                                                           format_time=format_time,
                                                           count=count,
                                                           hi=hi,
                                                           latest_block=latest_block_height,
                                                           chain_age=chain_age,
                                                           genesis_time=genesis_timestamp), 200
                elif form.search.data.startswith(tx_prefixes):
                    if 76 >= len(form.search.data) >= 6:
                        the_tx = ''.join(form.search.data.split(':')[1:])
                        tx_lookup = db.session.query(TXs).filter(TXs.txid.like(f"%{the_tx}%")).all()
                        if tx_lookup:
                            if len(tx_lookup) == 1:
                                return redirect(url_for('tx', transaction=tx_lookup[0].txid))
                            else:
                                return render_template('search_results.html',
                                                       searched_addresses=[],
                                                       searched_blocks=[],
                                                       searched_txs=tx_lookup)
                        else:
                            # TODO - set a variable for being unable to find specifically transactions
                            return render_template('index.html',
                                                   search_validated=False,
                                                   form=form,
                                                   front_page_blocks=front_page_items,
                                                   format_time=format_time,
                                                   count=count,
                                                   hi=hi,
                                                   latest_block=latest_block_height,
                                                   chain_age=chain_age,
                                                   genesis_time=genesis_timestamp), 200
                    else:
                        return render_template('index.html',
                                               input_too_short=True,
                                               form=form,
                                               front_page_blocks=front_page_items,
                                               format_time=format_time,
                                               count=count,
                                               hi=hi,
                                               latest_block=latest_block_height,
                                               chain_age=chain_age,
                                               genesis_time=genesis_timestamp), 200
                else:
                    try:
                        input_data = int(form.search.data)
                    except ValueError:
                        if 64 >= len(form.search.data) >= 6:
                            address_like = db.session.query(AddressSummary).filter(AddressSummary.address.ilike(f"%{form.search.data}%")).all()
                            address_len = len(address_like)
                            input_data = form.search.data.lower()
                            tx_like = db.session.query(TXs).filter(TXs.txid.like(f"%{input_data}%")).all()
                            tx_len = len(tx_like)
                            block_like = db.session.query(Blocks).filter(Blocks.hash.like(f"%{input_data}%")).all()
                            block_len = len(block_like)
                            if address_len + tx_len + block_len >= 2:
                                 return render_template('search_results.html',
                                                        searched_addresses=address_like,
                                                        searched_blocks=block_like,
                                                        searched_txs=tx_like)
                            elif address_len + tx_len + block_len == 1:
                                if address_len:
                                    return redirect(url_for('address', the_address=address_like[0].address))
                                elif tx_len:
                                    return redirect(url_for('tx', transaction=tx_like[0].txid))
                                elif block_len:
                                    return redirect(url_for('block', block_hash_or_height=block_like[0].hash))
                            else:
                                return render_template('index.html',
                                                       search_validated=False,
                                                       form=form,
                                                       front_page_blocks=front_page_items,
                                                       format_time=format_time,
                                                       count=count,
                                                       hi=hi,
                                                       latest_block=latest_block_height,
                                                       chain_age=chain_age,
                                                       genesis_time=genesis_timestamp), 200
                    else:
                        if input_data in range(0, latest_block_height + 1):
                            return redirect(url_for('block', block_hash_or_height=input_data))
                        else:
                            return render_template('index.html',
                                                   search_validated=False,
                                                   form=form,
                                                   front_page_blocks=front_page_items,
                                                   format_time=format_time,
                                                   count=count,
                                                   hi=hi,
                                                   latest_block=latest_block_height,
                                                   chain_age=chain_age,
                                                   genesis_time=genesis_timestamp), 200
            else:
                return render_template('index.html',
                                       input_too_short=True,
                                       form=form,
                                       front_page_blocks=front_page_items,
                                       format_time=format_time,
                                       count=count,
                                       hi=hi,
                                       latest_block=latest_block_height,
                                       chain_age=chain_age,
                                       genesis_time=genesis_timestamp), 200
        else:
            return render_template('index.html',
                                   form=form,
                                   front_page_blocks=front_page_items,
                                   format_time=format_time,
                                   count=count,
                                   hi=hi,
                                   latest_block=latest_block_height,
                                   chain_age=chain_age,
                                   genesis_time=genesis_timestamp), 200
    elif request.method in ['GET', 'HEAD', 'OPTIONS']:
        return render_template('index.html',
                               form=form,
                               front_page_blocks=front_page_items,
                               format_time=format_time,
                               count=count,
                               hi=hi,
                               latest_block=latest_block_height,
                               chain_age=chain_age,
                               genesis_time=genesis_timestamp), 200


@application.get("/address/")
def redirect_to_address():
    if coin_uniques['burn_address'] is not None:
        return redirect(url_for('address', the_address=coin_uniques['burn_address']))
    else:
        return redirect(url_for('address', the_address='INVALIDADDRESS'))


@application.get("/address/<the_address>")
@cache.memoize(300)
def address(the_address):
    # No reason to waste an SQL lookup if we're being redirected from /address/ ^
    if the_address == 'INVALIDADDRESS':
        return render_template('404.html', error="Not a valid address"), 404
    the_page = request.args.get('page', default=1, type=int)
    # Realistically there isn't going to be an address with 1,000,000,000 separate transactions.
    # If someone tries to go to page 1000000 or above, 403 them for strange behavior.
    # This is also done earlier to prevent an SQL lookup.
    if the_page >= 1000000:
        return render_template('404.html', error="Doing something weird?"), 403
    address_summary = db.session.query(AddressSummary).filter_by(address=the_address).one_or_none()
    if address_summary is None:
        if cryptocurrency.validateaddress(the_address)['isvalid']:
            return render_template('404.html', error="Address not seen on the network."), 404
        else:
            return render_template('404.html', error="Not a valid address"), 400
    else:
        address_count = address_summary.transactions_in + address_summary.transactions_out
        total_pages = math.ceil(address_count / 1000)
        if the_page > total_pages:
            the_page = total_pages
        if total_pages == 1:
            address_lookup = db.session.query(Addresses).filter_by(address=the_address).order_by(desc(Addresses.id))
            return render_template('address.html',
                                   address_info=address_lookup,
                                   the_address_summary=address_summary,
                                   this_address=the_address,
                                   total_balance=address_summary.balance,
                                   total_received=address_summary.received,
                                   total_sent=address_summary.sent,
                                   total_pages=total_pages,
                                   which_currency=coin_uniques["shortened"]), 200
        else:
            if the_page == 1:
                the_offset = 0
            else:
                the_offset = int((the_page - 1) * 1000)
            address_limited = db.session.query(Addresses).filter_by(address=the_address).order_by(desc(Addresses.id)).limit(1000).offset(the_offset)
            return render_template('address.html',
                                   address_info=address_limited,
                                   the_address_summary=address_summary,
                                   this_address=the_address,
                                   total_balance=address_summary.balance,
                                   total_received=address_summary.received,
                                   total_sent=address_summary.sent,
                                   the_page=the_page,
                                   total_pages=total_pages,
                                   which_currency=coin_uniques["shortened"]), 200


@application.get("/block/")
@cache.memoize(300)
def redirect_to_block():
    return redirect(url_for('block', block_hash_or_height="0"))


@application.get("/block/<block_hash_or_height>")
@cache.memoize(300)
def block(block_hash_or_height):
    try:
        the_block_height = int(block_hash_or_height)
    except ValueError:
        try:
            block_lookup = db.session.query(Blocks).filter_by(hash=block_hash_or_height.lower()).first()
            the_block_height = int(block_lookup.height)
        except(AttributeError, ValueError):
            return render_template('404.html', error="Not a valid block height/hash"), 404

    latest_block_height = int(db.session.query(Blocks).order_by(desc('height')).first().height)
    if the_block_height in range(0, latest_block_height + 1):
        the_block = db.session.query(Blocks).filter_by(height=the_block_height).first()
        if the_block is not None:
            block_hash = the_block.hash
            if the_block_height != 0:
                previous_block_hash = the_block.prevhash
            else:
                previous_block_hash = None

            if the_block_height != latest_block_height:
                next_block_hash = the_block.nexthash
            else:
                next_block_hash = None

            transactions = db.session.query(TXs).filter_by(block_height=the_block_height).all()
            txin = db.session.query(TXIn).filter_by(block_height=the_block_height).all()
            txout = db.session.query(TxOut).filter_by(block_height=the_block_height).all()

            return render_template('block.html',
                                   block_hash=block_hash,
                                   previous_block_hash=previous_block_hash,
                                   next_block_hash=next_block_hash,
                                   block_height=the_block_height,
                                   version=the_block.version,
                                   merkle_root=the_block.merkleroot,
                                   time=the_block.time,
                                   formatted_time=format_time(the_block.time),
                                   difficulty=the_block.difficulty,
                                   bits=the_block.bits,
                                   cumulative_difficulty=the_block.cumulative_difficulty,
                                   nonce=the_block.nonce,
                                   the_transactions=transactions,
                                   outstanding=the_block.outstanding,
                                   value_out=the_block.value_out,
                                   formatted_transaction_fees=format_eight_zeroes(the_block.transaction_fees),
                                   transaction_fees=the_block.transaction_fees,
                                   the_txin=txin,
                                   the_txout=txout,
                                   # TODO
                                   average_coin_age='?'), 200
        else:
            return render_template('404.html', error="Not a valid block height/hash"), 404
    else:
        return render_template('404.html', error="Not a valid block height/hash"), 404


@application.get("/tx/")
def redirect_to_tx():
    return redirect(url_for('tx', transaction="INVALID_TRANSACTION"))


@application.get("/tx/<transaction>")
@cache.memoize(300)
def tx(transaction):
    check_transaction = db.session.query(TXs).filter_by(txid=transaction.lower()).first()
    if check_transaction is not None:
        coinbase = db.session.query(CoinbaseTXIn).filter_by(txid=transaction.lower()).one_or_none()
        txin = db.session.query(TXIn).filter_by(txid=transaction.lower()).all()
        txout = db.session.query(TxOut).filter_by(txid=transaction.lower()).all()
        if txin is not None and txout is not None:
            block_height_lookup = db.session.query(Blocks).filter_by(height=check_transaction.block_height).first()
            return render_template('transaction.html',
                                   coinbase=coinbase,
                                   the_datetime=format_time(block_height_lookup.time),
                                   block_height=check_transaction.block_height,
                                   inputs=txin,
                                   outputs=txout,
                                   total_out=format_eight_zeroes(check_transaction.total_out),
                                   total_in=format_eight_zeroes(check_transaction.total_in),
                                   this_transaction=transaction.lower(),
                                   fee=format_eight_zeroes(check_transaction.fee),
                                   size=check_transaction.size), 200
        else:
            return render_template('404.html', error="Not a valid transaction"), 404
    else:
        return render_template('404.html', error="Not a valid transaction"), 404


@application.get("/api/")
@cache.memoize(86400)
def api_index():
    return render_template('api_index.html'), 200


@application.get("/api/addressbalance/")
@cache.memoize(300)
def redirect_to_api__address_balance():
    if coin_uniques['burn_address'] is not None:
        return redirect(url_for('api__address_balance', the_address=coin_uniques['burn_address']))
    else:
        return redirect(url_for('api__address_balance', the_address='INVALIDADDRESS'))


@application.get("/api/confirmations/")
def redirect_to_api__confirmations():
    return redirect(url_for('api__confirmations', userinput_block_height="0"))


@application.get("/api/rawtx/")
def redirect_to_api__rawtx():
    return redirect(url_for('api__rawtx', transaction="INVALIDTRANSACTION"))


@application.get("/api/receivedbyaddress/")
def redirect_to_api__received_by_address():
    if coin_uniques['burn_address'] is not None:
        return redirect(url_for('api__received_by_address', the_address=coin_uniques['burn_address']))
    else:
        return redirect(url_for('api__received_by_address', the_address='INVALIDADDRESS'))


@application.get("/api/sentbyaddress/")
def redirect_to_api__sent_by_address():
    if coin_uniques['burn_address'] is not None:
        return redirect(url_for('api__sent_by_address', the_address=coin_uniques['burn_address']))
    else:
        return redirect(url_for('api__sent_by_address', the_address='INVALIDADDRESS'))


@application.get("/api/validateaddress/")
def redirect_to_api__validate_address():
    if coin_uniques['burn_address'] is not None:
        return redirect(url_for('api__validate_address', the_address=coin_uniques['burn_address']))
    else:
        return redirect(url_for('api__validate_address', the_address='INVALIDADDRESS'))


@application.get("/api/addressbalance/<the_address>/")
@cache.memoize(300)
def api__address_balance(the_address):
    if the_address == "INVALID_ADDRESS":
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'Hi there, did you mean to put in an address?',
                                                               'error': '404'}))
    address_lookup = db.session.query(AddressSummary).filter_by(address=the_address).first()
    if address_lookup is None:
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'This address is invalid',
                                                               'error': '404'}))
    else:
        address_balance = address_lookup.balance
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps({'message': address_balance,
                                                               'error': 'ok'}))


@application.get("/api/blockcount/")
@cache.cached(timeout=120)
def api__block_count():
    most_recent_height = db.session.query(Blocks).order_by(desc('height')).first().height
    return application.response_class(mimetype='application/json',
                                      status=200,
                                      response=json.dumps({'message': most_recent_height,
                                                           'error': 'ok'}))


@application.get("/api/confirmations/<userinput_block_height>/")
@cache.memoize(300)
def api__confirmations(userinput_block_height):
    try:
        userinput_block_height = int(userinput_block_height)
    except ValueError:
        # not a block number, check if it's a hash
        try:
            block_lookup = db.session.query(Blocks).filter_by(hash=userinput_block_height.lower()).first()
            if block_lookup is not None:
                user_block_height = int(block_lookup.height)
                latest_block_height = int(db.session.query(Blocks).order_by(desc('height')).first().height)
                block_confirmations = (latest_block_height + 1) - user_block_height
                return application.response_class(mimetype='application/json',
                                                  status=200,
                                                  response=json.dumps({'confirmations': block_confirmations,
                                                                       'error': 'ok'}))
            else:
                return application.response_class(mimetype='application/json',
                                                  status=422,
                                                  response=json.dumps({'message': 'This block hash/height is invalid',
                                                                       'error': 'invalid'}))
        except JSONRPCException:
            return application.response_class(mimetype='application/json',
                                              status=422,
                                              response=json.dumps({'message': 'This block hash/height is invalid',
                                                                   'error': 'invalid'}))
    else:
        latest_block_height = int(db.session.query(Blocks).order_by(desc('height')).first().height)
        # check if this is a block number like 0 or something else.
        # +1 because range() goes up to but doesn't include the number, so to include it we do +1
        if userinput_block_height in range(0, latest_block_height + 1):
            block_confirmations = (latest_block_height + 1) - userinput_block_height
            return application.response_class(mimetype='application/json',
                                              status=200,
                                              response=json.dumps({'confirmations': block_confirmations,
                                                                   'error': 'ok'}))
        else:
            return application.response_class(mimetype='application/json',
                                              status=422,
                                              response=json.dumps({'message': 'This block hash/height is invalid',
                                                                   'error': 'invalid'}))


@application.get("/api/connections/")
@cache.cached(timeout=600)
def api__connections():
    try:
        total_connections = cryptocurrency.getconnectioncount()
    except JSONRPCException:
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'There was a JSON error. Try again later',
                                                               'error': 'invalid'}))
    else:
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps({'message': total_connections,
                                                               'error': 'ok'}))


@application.get("/api/lastdifficulty/")
@cache.cached(timeout=120)
def api__last_difficulty():
    latest_difficulty = float(db.session.query(Blocks).order_by(desc('height')).first().difficulty)
    return application.response_class(mimetype='application/json',
                                      status=200,
                                      response=json.dumps({'message': str(latest_difficulty),
                                                           'error': 'ok'}))


@application.get("/api/mempool/")
@cache.cached(timeout=120)
def api__mempool():
    try:
        the_mempool = cryptocurrency.getrawmempool(True)
    except JSONRPCException:
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'There was a JSON error. Try again later',
                                                               'error': 'invalid'}))
    else:
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps(the_mempool))


@application.get("/api/peers/")
@cache.cached(timeout=900)
def api__peers():
    try:
        peers = cryptocurrency.getpeerinfo()
        for peer_num, each_peer in enumerate(peers):
            peers[peer_num]['subver'] = peers[peer_num]['subver'].strip('/')
    except JSONRPCException:
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'There was a JSON error. Try again later',
                                                               'error': 'invalid'}))
    else:
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps(peers))


@application.get("/api/rawtx/<transaction>/")
@cache.memoize(300)
def api__rawtx(transaction):
    if transaction == "INVALIDTRANSACTION":
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'This transaction is invalid',
                                                               'error': 'invalid'}))
    try:
        the_transaction = cryptocurrency.getrawtransaction(transaction, 1)
    except JSONRPCException:
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'This transaction is invalid',
                                                               'error': 'invalid'}))
    else:
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps(the_transaction))


@application.get("/api/receivedbyaddress/<the_address>/")
@cache.memoize(300)
def api__received_by_address(the_address):
    if the_address == "INVALID_ADDRESS":
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'Hi there, did you mean to put in an address?',
                                                               'error': '404'}))
    address_lookup = db.session.query(AddressSummary).filter_by(address=the_address).first()
    if address_lookup is None:
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'This address is invalid',
                                                               'error': '404'}))
    else:
        address_received = str(address_lookup.received)
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps({'message': address_received,
                                                               'error': 'ok'}))


@application.get("/api/richlist/")
@cache.cached(timeout=3600)
def api__rich_list():
    the_top = db.session.query(AddressSummary).order_by(desc('balance')).limit(500)
    the_rich_list = {}
    for the_index, the_address in enumerate(the_top):
        the_rich_list[the_index] = {"address": the_address.address, "balance": the_address.balance}
    return application.response_class(mimetype='application/json',
                                      status=200,
                                      response=json.dumps({'message': the_rich_list, 'error': 'ok'}))


@application.get("/api/sentbyaddress/<the_address>/")
@cache.memoize(300)
def api__sent_by_address(the_address):
    if the_address == "INVALID_ADDRESS":
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'Hi there, did you mean to put in an address?',
                                                               'error': '404'}))
    address_lookup = db.session.query(AddressSummary).filter_by(address=the_address).first()
    if address_lookup is None:
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'This address is invalid',
                                                               'error': '404'}))
    else:
        address_sent = address_lookup.sent
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps({'message': address_sent,
                                                               'error': 'ok'}))


@application.get("/api/totalcoins/")
@cache.cached(timeout=300)
def api__total_coins():
    return application.response_class(mimetype='application/json',
                                      status=200,
                                      response=json.dumps({'message': str(cryptocurrency.gettxoutsetinfo()['total_amount']),
                                                           'error': 'ok'}))


@application.get("/api/totaltransactions/")
@cache.cached(timeout=300)
def api__total_transactions():
    return application.response_class(mimetype='application/json',
                                      status=200,
                                      response=json.dumps({'message': cryptocurrency.gettxoutsetinfo()['transactions'],
                                                           'error': 'ok'}))


@application.get("/api/validateaddress/<the_address>/")
@cache.memoize(300)
def api__validate_address(the_address):
    if the_address == "INVALID_ADDRESS":
        return application.response_class(mimetype='application/json',
                                          status=404,
                                          response=json.dumps({'message': 'Hi there, did you mean to put in an address?',
                                                               'error': '404'}))
    if cryptocurrency.validateaddress(the_address)['isvalid']:
        return application.response_class(mimetype='application/json',
                                          status=200,
                                          response=json.dumps({'message': 'valid',
                                                               'error': 'ok'}))
    else:
        return application.response_class(mimetype='application/json',
                                          status=422,
                                          response=json.dumps({'message': 'invalid',
                                                               'error': 'this string cannot be verified as an address'}))
