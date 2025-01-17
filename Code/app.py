# from werkzeug import secure_filename

from flask import *
from flask_restful import Resource, Api
from flask_pymongo import PyMongo
import pymongo
from flask_mongoengine import MongoEngine
from flask_security import Security, MongoEngineUserDatastore, \
    UserMixin, RoleMixin, login_required

import socket
import urllib
import json
import db_utils
import time
import ip_utils
import nmap
import utils.http as http_utils
import utils.openvas as ovas
import utils.burpsuite as burp
import re

from crontab import CronTab
from bson import json_util
import json
import domain_utils
from bson.json_util import dumps
from bson import ObjectId

import nmap_utils
import vuln_utils

from flask_celery import make_celery

import subdomain_enum_osint
import subdomain_enum_bruteforce
import billiard as multiprocessing



app = Flask(__name__, static_url_path='')
app.config.from_object('config')

app.url_map.strict_slashes = False
app.secret_key = 'qweoi@#!ASDQWEJKLZXCJ'
app.config['UPLOAD_FOLDER'] = 'upload/'
app.config['MAX_CONTENT_PATH'] = 2048
app.config['SQLALCHEMY_DATABASE_URI'] = ''
app.config['CELERY_BROKER_URI'] = 'redis://localhost:6379/0'
# app.config['CELERY_BACKEND'] = 'redis://localhost:6379/0'
mongo_user = "admin_db"
mongo_password = "long@2020"

app.config["MONGO_URI"] = "mongodb://%s:%s@192.168.33.10:27017/ThesisDB" % (mongo_user, urllib.parse.quote(mongo_password))

mongo = PyMongo(app)

# client = MongoClient("mongodb://%s:%s@192.168.33.10:27017" % (mongo_user, mongo_password))  # host uri
# db = client.ThesisDB  # Select the database
db = mongo.db
dm_clt = mongo.db.domain
ip_clt = mongo.db.ip
service_clt = mongo.db.service
vuln_clt = mongo.db.vuln
burp_clt = mongo.db.burp

api = Api(app)

todos = {}
celery = make_celery(app)

# JINJA_ENVIRONMENT.globals['STATIC_PREFIX'] = '/static/'

# MongoDB Config
app.config['MONGODB_DB'] = 'ThesisDB'
app.config['MONGODB_HOST'] = '192.168.33.10'
app.config['MONGODB_PORT'] = 27017
app.config['MONGODB_USERNAME'] = mongo_user
app.config['MONGODB_PASSWORD'] = mongo_password
app.config['SECURITY_PASSWORD_SALT'] = '!@#(ASDJZ)'

# Create database connection object
db_login = MongoEngine(app)

class Role(db_login.Document, RoleMixin):
    name = db_login.StringField(max_length=80, unique=True)
    description = db_login.StringField(max_length=255)

class User(db_login.Document, UserMixin):
    email = db_login.StringField(max_length=255)
    password = db_login.StringField(max_length=255)
    active = db_login.BooleanField(default=True)
    confirmed_at = db_login.DateTimeField()
    roles = db_login.ListField(db_login.ReferenceField(Role), default=[])

# Setup Flask-Security
user_datastore = MongoEngineUserDatastore(db, User, Role)
security = Security(app, user_datastore)

# Create a user to test with
@app.before_first_request
def create_user():
    user_datastore.create_user(email='miracle@dragon.com', password='1')

# @app.before_request
# @login_required
# def before_request():
# 	pass
# @app.before_request
# def check_valid_login():
#     login_valid = 'user' in session # or whatever you use to check valid login

#     if (request.endpoint and 
#         'static' not in request.endpoint and 
#         not login_valid and 
#         not getattr(app.view_functions[request.endpoint], 'is_public', False) ) :
#         return render_template('login.html', next=request.endpoint)

# Domain
class DomainListAPI(Resource):
	def get(self):
		headers = {'Content-Type': 'text/html'}
		domains = dm_clt.find()
		domains_json = db_utils.cursor_to_json(domains)
		return make_response(render_template('domain_dashboard.html', domains=domains_json, title='Domain Dashboard', active='domain'), 200, headers)

	def post(self):
		domain_name = request.form['domain_name']
		org_name = request.form['org_name'] if request.form['org_name'] is not None else ""
		ip_list = domain_utils.resolve(domain_name)
		wild_card = domain_utils.check_subdomain_wildcard(domain_name)
		brute_force = True if request.form['brute_force'] == 1 else False
		whois_data = domain_utils.whois(domain_name)

		# insert ip to db
		for ip in ip_list:
			status = ip_utils.check_alive(ip)
			whois_data = ip_utils.whois(ip)
			# update to db
			
			db.getCollection('ip').update(
				{"ip": ip},
				{'$setOnInsert': {'ip': ip, "status": status, 'whois': whois_data} },
				upsert=True
			)
		
		new_domain = {"domain_name": domain_name, "org_name": org_name, "ips": ip_list, 'wild_card': wild_card, "brute_force": brute_force, 'whois_data': whois_data}
		dm_clt.insert(new_domain)
		return redirect(url_for('target_dashboard'))


class SubDomainAPI(Resource):
	def get(self, domain_name):
		headers = {}
		domain_entity = dm_clt.find_one({'domain_name': domain_name})
		return make_response(render_template('subdomain_dashboard.html', domain=domain_entity, active='domain'), 200, headers)

	def post(self, domain_name):
		if request.form['_method'] == 'PUT':
			domain = dm_clt.find_one({'domain_name': domain_name})
			domain['domain_name'] = request.form['domain_name']
			domain['org_name'] = request.form['org_name']
			domain['ips'] = domain_utils.resolve(request.form['domain_name'])
			domain['wild_card'] = domain_utils.check_subdomain_wildcard(request.form['domain_name'])
			dm_clt.update_one({'_id' : domain['_id']}, {'$set': domain})
			return redirect(url_for('target_dashboard'))
		elif request.form['_method'] == 'DELETE':
			if request.form['type'] == 'sub':
				domain_entity = dm_clt.find_one({'domain_name': domain_name})
				domain_entity['subdomains'] = [x for x in domain_entity['subdomains'] if x['domain_name'] != request.form['domain_name']]
				dm_clt.update_one({'_id' : domain_entity['_id']}, {'$set': domain_entity})
				return redirect(url_for('api.subdomain', domain_name=domain_name))
			else:
				dm_clt.remove({'domain_name': domain_name})
				return redirect(url_for('target_dashboard'))



# IP
class IPListAPI(Resource):
	def get(self):
		headers = {'Content-Type': 'text/html'}
		ips = ip_clt.find()
		ips_json = ip_utils.cursor_to_json(ips)
		return make_response(render_template('ip_dashboard.html', ips=ips_json, title='IP Dashboard', active='ip'), 200, headers)

	def post(self):
		domain_name = request.form['domain_name']
		org_name = request.form['org_name'] if request.form['org_name'] is not None else ""
		ip_list = domain_utils.resolve(domain_name)
		wild_card = domain_utils.check_subdomain_wildcard(domain_name)
		brute_force = True if request.form['brute_force'] == 1 else False
		whois_data = domain_utils.whois(domain_name)
		
		new_domain = {"domain_name": domain_name, "org_name": org_name, "ips": ip_list, 'wild_card': wild_card, "brute_force": brute_force, 'whois_data': whois_data}
		dm_clt.insert(new_domain)
		
		# Check alive ip
		for ip in ip_list:
			status = ip_utils.check_alive(ip)
			# update to db
			
			db.getCollection('ip').update(
				{"ip": ip},
				{'$setOnInsert': {'ip': ip, "status": status} },
				upsert=True
			)
		# update status to db
		return redirect(url_for('target_dashboard'))

class IPAPI(Resource):
	def get(self, ip):
		headers = {}
		ip_entity = ip_clt.find_one({'ip': ip})
		scan_type_list = ['auth', 'broadcast', 'brute', 'default', 'discovery', 'dos', 'exploit', 'external', 'fuzzer', 'intrusive', 'malware', 'safe', 'version', 'vuln']
		return make_response(render_template('ip_detail.html', ip=ip_entity, scan_type_list=scan_type_list, title='IP Detail', active='ip'), 200, headers)

	def post(self, domain_name):
		if request.form['_method'] == 'PUT':
			domain = dm_clt.find_one({'domain_name': domain_name})
			domain['domain_name'] = request.form['domain_name']
			domain['org_name'] = request.form['org_name']
			domain['ips'] = domain_utils.resolve(request.form['domain_name'])
			domain['wild_card'] = domain_utils.check_subdomain_wildcard(request.form['domain_name'])
			dm_clt.update_one({'_id' : domain['_id']}, {'$set': domain})
			return redirect(url_for('target_dashboard'))
		elif request.form['_method'] == 'DELETE':
			if request.form['type'] == 'sub':
				domain_entity = dm_clt.find_one({'domain_name': domain_name})
				domain_entity['subdomains'] = [x for x in domain_entity['subdomains'] if x['domain_name'] != request.form['domain_name']]
				dm_clt.update_one({'_id' : domain_entity['_id']}, {'$set': domain_entity})
				return redirect(url_for('api.subdomain', domain_name=domain_name))
			else:
				dm_clt.remove({'domain_name': domain_name})
				return redirect(url_for('target_dashboard'))


class VulnListAPI(Resource):
	def get(self):
		headers = {'Content-Type': 'text/html'}
		vulns = vuln_clt.find({})
		vulns_json = vuln_utils.cursor_to_json(vulns)
		return make_response(render_template('vuln_dashboard.html', vulns=vulns_json, title='Vulnerability Dashboard', active='vuln'), 200, headers)

	def post(self):
		service = request.form['service']
		vuln_name = request.form['vuln_name'] if request.form['vuln_name'] is not None else ""
		product = request.form['product'] if request.form['product'] is not None else ""
		version = request.form['version'] if request.form['version'] is not None else ""
		script = request.form['script'] if request.form['script'] is not None else ""
		detail = request.form['detail'] if request.form['detail'] is not None else ""
		mitigation_methods = request.form['mitigation_methods'] if request.form['mitigation_methods'] is not None else ""

		available_exploit = True if request.form['available_exploit'] == 'yes' else False

		new_vuln = {"service": service, "vuln_name": vuln_name, "product": product, 'version': version, "script": script, 'detail': detail, 'mitigation_methods': mitigation_methods, 'available_exploit': available_exploit}
		vuln_clt.insert(new_vuln)

		return redirect(url_for('api.vulns'))

class VulnAPI(Resource):

	# push it back
	def get(self, vuln):
		headers = {'Content-Type': 'text/html'}
		vuln_entity = vuln_clt.find_one({'ip': vuln})
		scan_type_list = ['auth', 'broadcast', 'brute', 'default', 'discovery', 'dos', 'exploit', 'external', 'fuzzer', 'intrusive', 'malware', 'safe', 'version', 'vuln']
		return make_response(render_template('ip_detail.html', ip=ip_entity, scan_type_list=scan_type_list), 200, headers)

	# vuln scan 
	def post(self, vuln_id):
		if request.form['_method'] == 'PUT':
			vuln = vuln_clt.find_one({'_id': ObjectId(vuln_id)})

			vuln['service'] = request.form['service']
			vuln['vuln_name'] = request.form['vuln_name'] if request.form['vuln_name'] is not None else ""
			vuln['product'] = request.form['product'] if request.form['product'] is not None else ""
			vuln['version'] = request.form['version'] if request.form['version'] is not None else ""
			vuln['script'] = request.form['script'] if request.form['script'] is not None else ""
			vuln['detail'] = request.form['detail'] if request.form['detail'] is not None else ""
			vuln['mitigation_methods'] = request.form['mitigation_methods'] if request.form['mitigation_methods'] is not None else ""

			vuln['available_exploit'] = True if request.form['available_exploit'] == 'yes' else False
			
			vuln_clt.update_one({'_id' : vuln['_id']}, {'$set': vuln})
			return redirect(url_for('api.vulns'))
		elif request.form['_method'] == 'DELETE':
				vuln_clt.remove({'_id': ObjectId(vuln_id)})
				return redirect(url_for('api.vulns'))

class ServiceListAPI(Resource):
	def get(self):
		headers = {'Content-Type': 'text/html'}
		services = service_clt.find({})
		services_json = vuln_utils.cursor_to_json(services)
		return make_response(render_template('service_dashboard.html', services=services_json, title='Service Dashboard', active='service'), 200, headers)

	def post(self):
		# return request.form.getlist('protocol')
		service_name = request.form['service_name']
		description = request.form['description'] if request.form['description'] is not None else ""
		protocol = request.form.getlist('protocol')
		tls = request.form['tls'] if request.form['tls'] is not None else 0
		common_port = request.form['common_port'] if request.form['common_port'] is not None else ""

		new_serv = {"service_name": service_name, "description": description, 'protocol': protocol, 'tls': tls, 'common_port': common_port}
		service_clt.insert_one(new_serv)

		return redirect(url_for('api.services'))

class ServiceAPI(Resource):

	# push it back
	def get(self, vuln):
		headers = {'Content-Type': 'text/html'}
		vuln_entity = vuln_clt.find_one({'ip': vuln})
		scan_type_list = ['auth', 'broadcast', 'brute', 'default', 'discovery', 'dos', 'exploit', 'external', 'fuzzer', 'intrusive', 'malware', 'safe', 'version', 'vuln']
		return make_response(render_template('ip_detail.html', ip=ip_entity, scan_type_list=scan_type_list), 200, headers)

	# vuln scan 
	def post(self, service_id):
		if request.form['_method'] == 'PUT':
			# return request.form
			service = service_clt.find_one({'_id': ObjectId(service_id)})

			service['service_name'] = request.form['service_name']
			service['description'] = request.form['description'] if request.form['description'] is not None else ""
			service['protocol'] = request.form.getlist('protocol')
			service['tls'] = request.form['tls'] if request.form['tls'] is not None else 0

			service['common_port'] = request.form['common_port'] if request.form['common_port'] is not None else ""

			
			service_clt.update_one({'_id' : service['_id']}, {'$set': service})
			return redirect(url_for('api.services'))
		elif request.form['_method'] == 'DELETE':
			service_clt.remove({'_id': ObjectId(service_id)})
			return redirect(url_for('api.services'))


api.add_resource(DomainListAPI, '/targets', endpoint = 'api.domains')
api.add_resource(SubDomainAPI, '/targets/<string:domain_name>', endpoint = 'api.subdomain')

api.add_resource(IPListAPI, '/ips', endpoint = 'api.ips')
api.add_resource(IPAPI, '/ips/<string:ip>', endpoint = 'api.ip')

api.add_resource(VulnListAPI, '/vulns', endpoint = 'api.vulns')
api.add_resource(VulnAPI, '/vulns/<string:vuln_id>', endpoint = 'api.vuln')

api.add_resource(ServiceListAPI, '/services', endpoint = 'api.services')
api.add_resource(ServiceAPI, '/services/<string:service_id>', endpoint = 'api.service')

@app.route('/api/domains/list')
def domain_list():
	# headers = {'Content-Type': 'application/json'}
	domains = dm_clt.find()
	domains_json = db_utils.cursor_to_json(domains)
	return jsonify(domains_json)

@app.route('/domains/scan/<string:domain>')
def set_subdomain_scan_schedule(domain):
	schedule = request.form['schedule']
	my_jobs = CronTab(user='te')
	new_job = my_jobs.new(command='python3 /home/te/Projects/Thesis/Code/subdomain_enum.py')
	new_job.day.every(1)
	my_jobs.write()

@app.route('/')
@login_required
def home():
    num_domains = dm_clt.count()
    num_ips = ip_clt.count()
    num_vuln = vuln_clt.count()
    num_serv = service_clt.count()
    return render_template('home_page.html', title="Dragon Scanner", page_title='Home Page', num_domains=num_domains, num_ips=num_ips, num_vuln=num_vuln, num_serv=num_serv, active='main_dashboard')

@app.route('/static/<path:filename>')
def static_file():
    
    return

@app.route('/dashboard')
def domain_dashboard():
	return redirect(url_for('api.domains'))




@app.route('/targets/')
def target_dashboard():
	return redirect(url_for(('api.domains')))

@app.route('/targets/create')
def create_domain():
	return render_template('domain_create.html', title='Adding Domain')


@app.route('/targets/<string:domain_name>/edit')
def edit_domain(domain_name):
	# get domain entity for 
	domain_entity = dm_clt.find_one({'domain_name': domain_name})
	return render_template('targets_edit.html', edited_domain=domain_entity, title='Edit Target')


@app.route('/targets/<string:domain_name>/scan')
def subdomain_enumeration(domain_name):
	# push task to redis
	domain_entity = dm_clt.find_one({'domain_name': domain_name})

	# get current task and check
	task = subdomain_enum_worker.delay(domain_name)
	domain_entity['subdomain_enum_task_id'] = task.task_id
	dm_clt.update_one({'_id': domain_entity['_id']}, {'$set': domain_entity})
	return redirect(url_for('api.domains'))

@app.route('/targets/<string:domain_name>/brute_force_scan')
def subdomain_bruteforce(domain_name):
	# push task to redis
	domain_entity = dm_clt.find_one({'domain_name': domain_name})

	# get current task and check
	task = subdomain_bruteforce_worker.delay(domain_name)
	domain_entity['subdomain_enum_task_id'] = task.task_id
	dm_clt.update_one({'_id': domain_entity['_id']}, {'$set': domain_entity})
	return redirect(url_for('api.domains'))

@celery.task(name='app.subdomain_scan')
def subdomain_enum_worker(domain):
	subdomain_enum_osint.osint_update(domain)
	return

@celery.task(name='app.subdomain_bruteforce')
def subdomain_bruteforce_worker(domain):
	subdomain_enum_bruteforce.osint_update(domain)
	return










@app.route('/ips/create')
def create_ip():
	return render_template('ip_create.html')

@app.route('/ips/<string:ip>/edit')
def edit_ip(ip):
	return ip


@app.route('/ips/<string:ip>/scan')
def ip_scan(ip):
	ip_scan_worker.delay(ip)
	return redirect(url_for('api.ips'))

@celery.task(name='app.ip_scan')
def ip_scan_worker(ip):
	print ('Start regular scan on {}'.format(ip))
	nmap_utils.regular_scan_port(ip)
	return



# All port scan
@app.route('/full_port_scan/<string:ip>')
def full_port_scan(ip):
	full_port_scan_worker.delay(ip)
	return redirect(url_for('api.ip', ip=ip))

@celery.task(name='app.full_port_scan')
def full_port_scan_worker(ip):
	print ("Start running full port scan on {}...".format(ip))
	nmap_utils.vulners_script_scan(ip)
	return



# Vulner script scan
@app.route('/vulner_scan/<string:ip>')
def vuln_script_scan(ip):
	vuln_script_scan_worker.delay(ip)
	return redirect(url_for('api.ip', ip=ip))

@celery.task(name='app.vulner_scan')
def vuln_script_scan_worker(ip):
	print ("Start running full port scan on {}...".format(ip))
	nmap_utils.vulners_script_scan(ip)
	return



# vuln database
@app.route('/vulns/create')
def create_vuln():
	# get list service
	services = service_clt.find({})
	return render_template("vuln_create.html", services=services)

@app.route('/vulns/edit/<string:vuln_id>')
def edit_vuln(vuln_id):
	# get vuln in db
	vuln_entity = vuln_clt.find_one({'_id': ObjectId(vuln_id)})
	services = service_clt.find({})
	return render_template("vuln_edit.html", vuln=vuln_entity, services=services, title='Edit Vulnerability')

@app.route('/google_hacking_dashboard')
def google_hacking_dashboard():
	queries = {
		'1': 'Directory listing vulnerabilities',
		'2': 'Configuration files exposed',
		'3': 'Database files exposed',
		'4': 'Log files exposed',
		'5': 'Backup and old files',
		'6': 'Login pages'
	}
	queries_2 = {
		'7':'SQL errors',
		'8':'Publicly exposed documents',
		'9':'phpinfo()',
		'10':'PHP errors / warnings',
		'11':'Search Pastebin.com / pasting sites',
		'12':'Search Github.com and Gitlab.com',
		'13':'Search Stackoverflow.com'
	}
	return render_template('google_hacking_dashboard.html', queries=queries, queries_2=queries_2, title='Google Hacking Queries', active='google_dork')



# service database
@app.route('/services/create')
def create_service():
	return render_template("service_create.html")

@app.route('/services/edit/<string:service_id>')
def edit_service(service_id):
	# get service
	service_entity = service_clt.find_one({'_id': ObjectId(service_id)})
	return render_template('service_edit.html', service=service_entity)


@app.route('/detect_tech/<string:ip>')
def detect_tech(ip):
    detect_tech_worker.delay(ip)
    return redirect(request.referrer)

@celery.task(name='app.detect_tech')
def detect_tech_worker(ip):
    http_utils.detect_tech(ip)
    return



@app.route('/visualization/<string:domain>')
def visualization(domain):
	# screen shot with aquatone and rename file + change location
	pass

@app.route('/postscan/<string:ip>')
def port_scan(ip):
	# scan with 2 modules
	# portscan(ip)
	pass

@app.route('/servicescan/<string:ip>')
def service_scan(ip):
	# run scan with service scan

	# detect web technology with whatweb
	# use wappanalyzer
	pass

@app.route('/script_scan/<string:ip>')
def script_scan(ip):
	script_scan_worker.delay(ip)
	return redirect(request.referrer)


@app.route('/script_scan/<string:ip>')
def category_script_scan(ip):
	category_script_scan_worker.delay(ip)
	return redirect(request.referrer)


@celery.task(name='app.default_script_scan')
def script_scan_worker(ip):
	nmap_utils.default_script_scan(ip)
	return redirect(request.referrer)

@celery.task(name='app.default_script_scan')
def category_script_scan_worker(ip):
	nmap_utils.default_script_scan(ip)
	return


@app.route('/custom_script_scan/<string:ip>', methods=['POST'])
def custom_script_scan(ip):
	scan_type_list = request.form.getlist('scan_type[]')
	custom_script_scan_worker.delay(ip, scan_type_list)
	return redirect(request.referrer)

@celery.task(name='app.custom_script_scan')
def custom_script_scan_worker(ip, scan_type_list):
	print ("Running on ip: {}, Category: {}".format(ip, ','.join(scan_type_list)))
	nmap_utils.category_script_scan(ip, scan_type_list)
	return

@app.route('/vuln_scan/<string:ip>')
def vuln_scan(ip):
	vuln_scan_woker.delay(ip)
	return redirect(request.referrer)

@celery.task(name='app.vuln_scan')
def vuln_scan_woker(ip):
	print ("Start vuln script scan on {}.".format(ip))
	nmap_utils.vuln_scan(ip)
	return

@app.route('/brute_force_credentials/<string:domain>')
def brute_credentials(domain):
	# brute force with brutespray

	# get nmap result
	pass

@celery.task(name='app.os_detect')
def os_detect(ip):
	print ("OS detect on {}".format(ip))
	nmap_utils.os_detect(ip)
	return 'Done'

@celery.task(name='app.screenshot')
def screenshot_worker(list_http_serv):
	http_utils.screenshot_list(list_http_serv)
	return 'Screenshot success'

@app.route('/test/<string:ip>/<string:protocol>/<int:port>', methods=['POST', 'GET'])
def test_os(ip, protocol, port):
	http_utils.screenshot(ip, port, protocol)
	return 'Screenshot Success'

@app.route('/screenshot/')
def screenshot():
	res = http_utils.get_all_http_serv()
	# screenshot
	screenshot_worker.delay(res)
	return redirect(request.referrer)

@app.route('/scan/openvas/<string:target>')
def openvas_scan(target):
	scan_id, target_id = ovas.luanch_simple_scanner(target)
	# save to db
	ip_ent = ip_clt.find_one({'ip': target})
	ip_ent['openvan_scan_id'] = scan_id
	ip_ent['openvas_target_id'] = target_id
	ip_clt.update_one({'_id': ip_ent['_id']}, {'$set': ip_ent})
	return

@app.route('/burp_scan_dashboard')
def burp_scan_dashboard(target):
	return render_template('burp_dashboard.html')

@app.route('/scan/burp/<string:url>')
def burp_scan(url):
	task_id = burp.scan(url)
	if task_id is None:
		return
	
	burp_clt.insert_one({'url': url, 'task_id': task_id})
	return








@app.route('/test/', methods=['POST', 'GET'])
@login_required
def test():
	ip_ent = ip_clt.find_one({'ip': '52.77.36.99'})
	return ip_ent['scan']['52.77.36.99']['tcp']['443']

@app.route('/images/screenshots/<path:path>')
def send_images(path):
	return send_from_directory('screenshots', path)

if __name__ == '__main__':
	app.debug = True
	app.run()