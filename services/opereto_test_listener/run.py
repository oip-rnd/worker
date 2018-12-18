from opereto.helpers.services import ServiceTemplate
from opereto.utils.validations import JsonSchemeValidator, default_variable_name_scheme, default_entity_name_scheme, process_result_keys, process_status_keys
from opereto.utils.osutil import get_file_md5sum, remove_directory_if_exists, make_directory
from opereto.exceptions import raise_runtime_error
from pyopereto.client import OperetoClient
from opereto.exceptions import *
import os, time, json


class ServiceRunner(ServiceTemplate):

    def __init__(self, **kwargs):
        self.client = OperetoClient()
        ServiceTemplate.__init__(self, **kwargs)
        self._print_step_title('Start opereto test listener..')

    def validate_input(self):

        input_scheme = {
            "type": "object",
            "properties" : {
                "test_results_path": {
                    "type" : "string",
                    "minLength": 1
                }
            },
            "required": ['test_results_path'],
            "additionalProperties": True
        }

        validator = JsonSchemeValidator(self.input, input_scheme)
        validator.validate()

        self.test_pid = self.input['opereto_parent_flow_id'] or self.input['pid']
        self.test_results_dir = self.input['test_results_path']

        if not os.path.exists(self.test_results_dir):
            raise_runtime_error('Test results directory {} does not exist.'.format(self.test_results_dir))

        self.result_keys = process_result_keys
        self.status_keys = process_status_keys

        self.tests_json_scheme = {
            "type": "object",
            "properties": {
                "test_suite": {
                    "type": "object",
                    "properties": {
                        "links": {
                            "type": "array"
                        },
                        "status": {
                            "enum": self.status_keys
                        }
                    }
                },
                "test_records": {
                    "type": "array",
                    "items": [
                        {
                            "type": "object",
                            "properties": {
                                "testname": default_variable_name_scheme,
                                "status": {
                                    "enum": self.status_keys
                                },
                                "title": default_entity_name_scheme,
                                "links": {
                                    "type": "array",
                                    "items": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "url": {
                                                    "type": "string"
                                                },
                                                "name": {
                                                    "type": "string"
                                                }
                                            }
                                        }
                                    ]
                                }
                            },
                            "required": ['testname', 'status'],
                            "additionalProperties": True
                        }
                    ]
                }
            },
            "additionalProperties": True
        }

        self.end_of_test_suite=False
        self.test_suite_final_status = 'success'
        self.test_data = {}
        self.suite_links = []
        self._state = {}

    def _print_test_link(self, link):
        print('[OPERETO_HTML]<br><a href="{}"><font style="color: #222; font-weight: 600; font-size: 13px;">{}</font></a>'.format(link['url'], link['name']))


    def _append_to_process_log(self, pid, loglines):
        log_request_data = {
            'sflow_id': self.input['opereto_source_flow_id'],
            'pflow_id': self.input['opereto_parent_flow_id'],
            'agent_id': self.input['opereto_agent'],
            'product_id': self.input['opereto_product_id'],
            'data': []
        }
        count = 1
        for line in loglines:
            try:
                millis = int(round(time.time() * 1000)) + count
                log_request_data['data'].append({
                    'level': 'info',
                    'text': line.strip(),
                    'timestamp': millis
                })
            except Exception, e:
                print e
            count += 1

        self.client._call_rest_api('post', '/processes/{}/log'.format(pid), data=log_request_data,
                                       error='Failed to update test log (test pid = {})'.format(pid))


    def _modify_record(self, test_record):

        testname = test_record['testname']
        test_input = test_record.get('test_input') or {}
        title = test_record.get('title') or testname
        status = test_record['status']
        test_links = test_record.get('links') or []

        if testname not in self._state:
            pid = self.client.create_process('opereto_test_listener_record', testname=testname, title=title, test_input=test_input)
            self._state[testname] = {
                'pid': pid,
                'status': 'in_process',
                'title': title,
                'test_output_md5': '',
                'summary_md5': '',
                'last_log_line': 1
            }
            time.sleep(2)
        else:
            pid = self._state[testname]['pid']

        if self._state[testname]['status'] not in self.result_keys:

            if title!=self._state[testname]['title']:
                ### TBD: add title change API call
                self._state[testname]['title']=title

            results_dir = os.path.join(self.test_results_dir, testname)
            if os.path.exists(results_dir):
                output_json_file = os.path.join(results_dir, 'output.json')
                log_file = os.path.join(results_dir, 'log.txt')
                summary_file = os.path.join(results_dir, 'summary.txt')

                if os.path.exists(output_json_file):
                    output_json_md5 = get_file_md5sum(output_json_file)
                    with open(output_json_file, 'r') as of:
                        output_json = json.load(of)
                        if output_json_md5!=self._state[testname]['test_output_md5']:
                            self.client.modify_process_property('test_output', output_json, pid=pid)
                            self._state[testname]['test_output_md5'] = output_json_md5

                if os.path.exists(summary_file):
                    summary_md5 = get_file_md5sum(summary_file)
                    with open(summary_file, 'r') as sf:
                        summary = sf.read()
                        if summary_md5!=self._state[testname]['summary_md5']:
                            self.client.modify_process_summary(pid, summary)
                            self._state[testname]['summary_md5'] = summary_md5

                if os.path.exists(log_file):
                    with open(log_file, 'r') as lf:
                        count=1
                        loglines=[]
                        for line in lf.readlines():
                            if count>=self._state[testname]['last_log_line']:
                                if count>9900:
                                    message = 'Test log is too long. Please save test log in remote storage and add a link to it in Opereto log. See service info to learn how to add links to your tests.json file.'
                                    loglines.append('[OPERETO_HTML]<br><br><font style="width: 800px; padding: 15px; color: #222; font-weight: 400; border:2px solid red; background-color: #f8f8f8;">{}</font><br><br>'.format(message))
                                    break
                                loglines.append(line.strip())
                            count+=1
                        self._append_to_process_log(pid, loglines)
                        self._state[testname]['last_log_line']=count

            if status in self.result_keys:
                links=[]
                for link in test_links:
                    html_link = '[OPERETO_HTML]<br><a href="{}"><font style="color: #222; font-weight: 600; font-size: 13px;">{}</font></a>'.format(
                        link['url'], link['name'])
                    links.append(html_link)
                    self._append_to_process_log(pid, links)
                self.client.stop_process(pid, status=status)
                self._state[testname]['status']=status


    def process(self):

        def process_results():
            tests_json = os.path.join(self.test_results_dir, 'tests.json')
            if os.path.exists(tests_json):
                with open(tests_json, 'r') as tf:
                    self.test_data = json.load(tf)
                    all_tests_completed=True
                    try:
                        validator = JsonSchemeValidator(self.test_data, self.tests_json_scheme)
                        validator.validate()
                    except Exception, e:
                        print 'Invalid tests json file: {}'.format(e)
                        return

                    if 'test_records' in self.test_data:
                        for test_record in self.test_data['test_records']:
                            self._modify_record(test_record)
                            if test_record['status'] not in self.result_keys:
                                all_tests_completed=False

                    if 'test_suite' in self.test_data and all_tests_completed:
                        if 'status' in self.test_data['test_suite']:
                            if self.test_data['test_suite']['status'] in self.result_keys:
                                self.end_of_test_suite=True
                                self.test_suite_final_status=self.test_data['test_suite']['status']
                        if 'links' in self.test_data['test_suite']:
                            self.suite_links = self.test_data['test_suite']['links']


        while(True):
            process_results()
            time.sleep(20)
            if self.end_of_test_suite:
                break

        for link in self.suite_links:
            self._print_test_link(link)

        if self.test_suite_final_status=='success':
            return self.client.SUCCESS
        elif self.test_suite_final_status=='failure':
            return self.client.FAILURE
        elif self.test_suite_final_status=='warning':
            return self.client.WARNING
        else:
            return self.client.ERROR


    def setup(self):
        make_directory(self.input['test_results_path'])

    def teardown(self):
        remove_directory_if_exists(self.input['test_results_path'])


if __name__ == "__main__":
    exit(ServiceRunner().run())
