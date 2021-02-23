#!/usr/bin/env python
import subprocess
import tempfile
import re
import os
import json
import requests
import traceback
import pytest
from lib.reports import XMLReport, JSONReport
from jsonschema import validate, draft7_format_checker, Draft7Validator, RefResolver
from subprocess import Popen, check_output, PIPE, CalledProcessError
from time import sleep
from datetime import datetime
from pprint import pprint
from .utils import colors, enum, shell, datastate_to_ssh

# Error handling mode in scenario
Err = enum('CONTINUE', 'BREAK', 'FINALLY', 'IGNORE')

class ScenarioInterface:
  """ Holds a scenario data
  Most scenario related methods are global and not in this class to make scenario writing look like script writing
  """
  def __init__(self, name, datastate, schema={}, scenario_input={}):
    self.name = name
    self.stop = False
    self.errors = False
    self.datastate = datastate
    self.scenario_input = scenario_input
    self.rspec = "ruby -S rspec --order defined --fail-fast"
    self.token = ""
    self.schema = schema
    if not self.validate_schema():
      raise ValueError("The given datastate is not compatible with the expected platform schema")
    self.__set_token()

  def __set_token(self):
    try:
      self.token = self.ssh_on(self.nodes("server")[0], "cat /var/rudder/run/api-token")[1]
    except:
      self.token = ""

  # Validate that the given datastate is compatible with the scenario specific
  # required platform
  # TODO try to ssh on each host?
  def validate_schema(self):
    # Count each expected type
    found = {}
    result = True

    # Stored errors are dict on the following form:
    # { "message": "", "type": "<schema type>"}
    entries = { k: { "type": "unknow", "err": []} for k in self.datastate.keys()}
    missing_entries = list(self.schema.keys())
    for k in self.schema.keys():
      found[k] = 0
    try:
        # TODO take it from repo
        with open("rudder.jsonschema", "r") as json_file:
          rudder_schema = json.load(json_file)
        resolver = RefResolver.from_schema(rudder_schema)
        # Iterate over input to compare with schema
        for data_key, data_entry in self.datastate.items():
          for schema_key, schema_entry in self.schema.items():
            try:
              validate(instance=data_entry, schema=schema_entry["schema"], format_checker=draft7_format_checker, resolver=resolver)
              found[schema_key] = found[schema_key] + 1
              entries[data_key]["type"] = schema_key
            except Exception as e:
              entries[data_key]["err"].append({ "message":e, "type":schema_key})

        # Compare with expected occurences
        for k in self.schema.keys():
          if "min" in self.schema[k] and self.schema[k]["min"] > found[k]:
            print("Expected at least %s %s, but found %s"%(self.schema[k]["min"], k, found[k]))
            result = False
          elif "max" in self.schema[k] and self.schema[k]["max"] < found[k]:
            print("Expected at most %s %s, but found %s"%(self.schema[k]["max"], k, found[k]))
            result = False
          else:
            missing_entries.remove(k)

        # Display missing schema
        if result == False:
          # Print failures for the first error
          print("\n")
          first_wrong_entry = [{ "name": k, "value": entries[k]} for k in entries.keys() if entries[k]["type"] == "unknow"][0]
          print("ERROR for data entry %s:"%first_wrong_entry["name"])
          print("Could be due to one of the following errors:")
          for i in first_wrong_entry["value"]["err"]:
            if i["type"] == missing_entries[0]:
              print(i["message"])

          # Print parsing resume
          print("\n")
          print("Parsing resume:")
          to_print = { k: entries[k]["type"] for k in entries.keys()}
          print(json.dumps(to_print, indent=2, sort_keys=True))


    except Exception as err:
      print(err)
      result = False
    finally:
      return result


  def nodes(self, kind = "all"):
    # kind not defined, return all nodes
    if (kind == "all"):
      return self.datastate.keys()
    else:
      nodes = []
      for hostname, host in self.datastate.items():
        if host.get("role", "None") == kind:
          nodes.append(hostname)
      return nodes

  def host_rudder_version(self, hostname):
    version_line = self.ssh_on(hostname, "rudder agent version")
    match = re.match(r'^Rudder agent (\d+)\.(\d+)\..*', version_line[1])
    if match:
      return (match.group(1), match.group(2))
    else:
      return ("", "")

  def ssh_windows(self, host, command, live_output=False):
    # Hackish way on windows, dump to file, push it ont the agent and then execute it
    # Retrieve the temp folder path
    infos = self.datastate[host]
    default_ssh_options = ["StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null"]
    options = "-o \"" + "\" -o \"".join(default_ssh_options) + "\""

    getTmp = "\$env:TEMP"
    tmp = shell("ssh -i %s %s@%s -p %s %s \"%s\""%(infos["ssh_cred"], infos["ssh_user"], infos["ip"], infos["ssh_port"], options, getTmp), print_command=False)[1].strip()

    local_path = self.workspace + "/cmd_file.ps1"
    remote_path = tmp + "/cmd_file.ps1"
    with open(local_path, "w") as cmd_file:
        cmd_file.write(command)
    self.push_on(host, local_path, remote_path, print_command=False)
    return shell("ssh -i %s %s@%s -p %s %s \"%s\""%(infos["ssh_cred"], infos["ssh_user"], infos["ip"], infos["ssh_port"], options, remote_path), live_output=live_output)

  def ssh_unix(self, host, command, live_output=False):
      if host == "localhost":
          ssh_cmd = command
      else:
          infos = self.datastate[host]
          default_ssh_options = ["StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null"]
          options = "-o \"" + "\" -o \"".join(default_ssh_options) + "\""
          command = "sudo /bin/sh -c 'PATH=\\$PATH:/vagrant/scripts LANG=C " + command + "'"
          ssh_cmd = "ssh -i %s %s@%s -p %s %s \"%s\""%(infos["ssh_cred"], infos["ssh_user"], infos["ip"], infos["ssh_port"], options, command)
      return shell(ssh_cmd, live_output=live_output)

  def ssh_on(self, host, command, live_output=False):
      if host == "localhost":
          return self.ssh_unix(host, command)

      infos = self.datastate[host]
      if "windows" in infos['system']:
          return self.ssh_windows(host, command, live_output=live_output)
      else:
          return self.ssh_unix(host, command, live_output=live_output)

  def push_on(self, host, src, dst, recursive=False, print_command=True):
      if host == "localhost":
          if recursive:
            command = "cp %s %s"%(src, dst)
          else:
            command = "cp -r %s %s"%(src, dst)
      else:
          infos = self.datastate[host]
          default_ssh_options = ["StrictHostKeyChecking=no", "UserKnownHostsFile=/dev/null"]
          options = "-o \"" + "\" -o \"".join(default_ssh_options) + "\""
          if recursive:
            command = 'scp -r -i %s -P%s %s  "%s" "%s@%s:\\"%s\\""'%(infos["ssh_cred"], infos["ssh_port"], options, src, infos["ssh_user"], infos["ip"], dst)
          else:
            # The horrendous syntax is to be fully compatible with windows path, and path using spaces
            # It should run something like:  scp abc "Administrator@34.240.38.95:\"C:/Program Files/Rudder\""
            command = 'scp -i %s -P%s %s  "%s" "%s@%s:\\"%s\\""'%(infos["ssh_cred"], infos["ssh_port"], options, src, infos["ssh_user"], infos["ip"], dst)
      return shell(command, print_command=print_command)

  def start(self):
    tmpdir = "/tmp/rtf_scenario"
    self.start = datetime.now().isoformat()
    os.makedirs(tmpdir, exist_ok=True)
    self.workspace = tempfile.mkdtemp(dir=tmpdir)
    self.report = JSONReport(self.workspace + "/result.json", self.workspace)
    #self.report = XMLReport(self.workspace + "/result.xml", self.workspace)
    print(colors.YELLOW + "[" + self.start + "] Begining of scenario " + self.name + colors.RESET)

  def finish(self):
    """ Finish a scenario """
    self.end = datetime.now().isoformat()
    import shutil
    shutil.copyfile(self.report.path, "./result.json")
    shutil.rmtree(self.workspace, ignore_errors=True)
    print(colors.YELLOW + "[" + self.end + "] End of scenario" + colors.RESET)

  # If there's been error in scenario, then only the test with Err.FINALLY must be run
  def should_run(self, mode):
    """ Return True when the test must be ran """
    if not self.stop:
      return True

    if mode != Err.FINALLY:
      return False
    else: # mode == Err.FINALLY
      return self.run_finally

  def get_uuid(self, node):
    return str(self.ssh_on(node, "cat /opt/rudder/etc/uuid.hive")[1]).rstrip()

  ############################################
  # Commands to be used in a scenario script #
  ############################################

  """
    Run one test in a scenario and rudder_log <rudder_log> log file if it fails
    error_mode can be :
     - CONTINUE: continue testing even if this fail, should be the default
     - BREAK: stop the scenario if this fail, for tests that change a state
     - FINALLY: always run this test, for cleaning after a scenario, broken or not
     - IGNORE: will ignore the test test result in the global testing result

     If the test starts with a /, then the full path of the test will be used,
     otherwise it will look for a ruby script in specs/tests directory
  """
  def run(self, target, test, error_mode=Err.CONTINUE, **kwargs):
    print(colors.BLUE + "Running test %s on %s"%(test, target) + colors.RESET)

    if not self.should_run(error_mode):
      return

    # prepare command
    datastate_file = self.workspace + "/datastate.json"
    with open(datastate_file, 'w+') as outfile:
      json.dump(self.datastate, outfile)
    env = 'WORKSPACE="%s" '%(self.workspace + "/")
    if target != "localhost" and not target in self.datastate.keys():
      return
    env += 'TARGET_HOST=%s '%target
    env += 'TOKEN=%s '%(self.token)
    for k,v in kwargs.items():
      env += 'RUDDER_' + k + '=' + "'" + v + "' "
    if test.startswith("/"):
      testfile = test
      test = re.sub(r'.*/([\w\-]+)\.rb', r'\1', test)
    else:
      testfile = "spec/tests/" + test + ".rb"
    command = env + self.rspec + " " + testfile + " 2>/dev/null"

    # run it
    print("+%s"%command)
    process = Popen(command, shell=True)
    retcode = process.wait()

    self.report.merge_reports(self.name)

    if retcode != 0:
      if error_mode != Err.IGNORE:
        self.errors = True
        if error_mode == Err.BREAK:
          self.stop = True
      return retcode
    else:
      return 0

  """
    All args are passed in a serialized json named test_data
  """
  def run_testinfra(self, target, test, error_mode=Err.CONTINUE, **kwargs):
    input_data = {}
    for k,v in kwargs.items():
      input_data[k.lower()] = v
    print(colors.BLUE + "Running test %s on %s with:\n%s"%(test, target, json.dumps(input_data)) + colors.RESET)
    # prepare command
    testfile = "testinfra/tests/" + test + ".py"
    ssh_config_file =  self.workspace + "/ssh_config"
    datastate_to_ssh(target, self.datastate[target], ssh_config_file)
    try:
      webapp_url = self.datastate[self.nodes("server")[0]]["webapp_url"]
    except:
      webapp_url = ""

    tmp_report_file = self.workspace + "/tmp_report.json"
    pytest_cmd = ['--capture=no', '-v', '--test_data=%s'%json.dumps(input_data), '--token=%s'%self.token, '--webapp_url=%s'%webapp_url, testfile, "--json-report", "--json-report-file=" + tmp_report_file]
    if target != "localhost":
        pytest_cmd = ["--ssh-config=%s"%ssh_config_file,  "--hosts=%s"%target] + pytest_cmd
    pytest_cmd = ['pytest'] + pytest_cmd
    print("+%s"%" ".join(pytest_cmd))

    retcode = subprocess.call(pytest_cmd)
    self.report.merge_reports(self.name, new_report=tmp_report_file, input_data=input_data, datastate=self.datastate, scenario_input=self.scenario_input)

    if retcode != 0:
      if error_mode != Err.IGNORE:
        self.errors = True
        if error_mode == Err.BREAK:
          self.stop = True
      return retcode
    else:
      return 0

  def host_date(self, server, error_mode=Err.CONTINUE):
    """ Return the current date on the host """
    return self.ssh_on(server, "date +%s%3N")[1]


  def wait_for_generation(self, server, date0, hostname, timeout=10, error_mode=Err.CONTINUE, ):
    """ Wait for the generation of a given node promises """
    # wait for promise generation
    agent_uuid = self.get_uuid(hostname)
    if agent_uuid == "null":
      return
    time=0
    while True:
      time += 1
      if time >= timeout:
        break
      sleep(1)
      print("Waiting for " + agent_uuid + " rule generation")
      generated = "/var/rudder/share/" + agent_uuid + "/rules/cfengine-community/rudder-promises-generated"
      cmd = "cat " + generated + " 2>/dev/null | head -n1"
      (retcode, datestr) = self.ssh_on(server, cmd)
      if datestr == "":
        continue
      if re.match(r'^\d+$', datestr):
        date = datestr
      else:
        (retcode, date) = shell("date -d " + datestr + " +%s%3N")
      if int(date) > int(date0):
        break
    if time >= timeout:
      print("ERROR: Timeout in promise generation (>" + str(timeout) + "s)")

