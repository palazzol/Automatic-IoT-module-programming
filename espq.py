import os
import sys
import json
import datetime
import filecmp
import shutil
from re import sub, findall
from time import sleep
from copy import deepcopy
from random import randint
from subprocess import call
from ast import literal_eval
import paho.mqtt.client as mqtt

###########################################################
# Make sure these are set correctly for your environment:

site_config = 'sites.json'
blank_defines = 'blank_defines.h'

espqdir = os.path.dirname(os.path.abspath(__file__))

current_tasmota_version = '0x06060007'
tasmotadir = os.path.join(espqdir, '../Sonoff-Tasmota')
custom_dir = os.path.join(espqdir, '../custom-mqtt-programs/')

###########################################################


hass_template_dir = os.path.join(espqdir, 'hass_templates')
hass_output_dir = os.path.join(espqdir, 'hass_output')

loop_time, wait_time = 1.0, 45.0

colors = {"RED": '\033[1;31m',
          "GREEN": '\033[1;32m',
          "BLUE": '\033[94m',
          "NOCOLOR": '\033[0m'}

wifi_attr = {'status_payload': '11',
             'stat_topic': 'STATUS11',
             'values': {'reported_ssid': ['StatusSTS', 'Wifi', 'SSId'],
                        'reported_bssid': ['StatusSTS', 'Wifi', 'BSSId'],
                        'reported_channel': ['StatusSTS', 'Wifi', 'Channel'],
                        'reported_rssi': ['StatusSTS', 'Wifi', 'RSSI']}}
network_attr = {'status_payload': '5',
                'stat_topic': 'STATUS5',
                'values': {'reported_ip': ['StatusNET', 'IPAddress'],
                           'reported_mac': ['StatusNET', 'Mac'],
                           'reported_wificonfig':['StatusNET', 'WifiConfig']}}
firmware_attr = {'status_payload': '2',
                'stat_topic': 'STATUS2',
                'values': {'tas_version': ['StatusFWR', 'Version'],
                           'core_version': ['StatusFWR', 'Core']}}

class device(dict):
    """
        Structure to store the details of each hardware device
    """
    def __init__(self, device):
        for key in device:
            # Each devices doesn't need to know about the other IDs
            if key != 'ids':
                # Convert all other keys to device attributes
                # Sanitize attributes so missing ones are '' instead of None
                if device[key] is not None:
                    setattr(self, key, device[key])
                else:
                    setattr(self, key, '')
        if 'base_topic' in device and 'topic' in device:
            self.base_topic = self.base_topic.lower()
            self.topic = self.topic.lower()
            topic_template = '%prefix%/{base_topic}/{topic}'
            self.full_topic = topic_template.format(**self)
        self.c_topic = sub('%prefix%', 'cmnd', self.full_topic)
        self.s_topic = sub('%prefix%', 'stat', self.full_topic)
        self.t_topic = sub('%prefix%', 'tele', self.full_topic)
        self.f_name = sub('_', ' ', self.name)  # Friendly name
        self.name = self.name.replace(' ', '_')    # Unfriendly name

        self.name = self.name.lower()   # home assistant doesn't like upper case letters in sensor names

        # Insert site information as device attributes
        with open(site_config, 'r') as f:
            sites = json.load(f)
        for key in sites[self.site]:
            # Sanitize attributes so missing ones are '' instead of None
            if sites[self.site][key] is not None:
                setattr(self, key, sites[self.site][key])
            else:
                setattr(self, key, '')
        # Format the device's build flags
        # This is still a little hacky--tasmota and custom build flags are handled entirely differently
        # The latter way is probably better since it's the general case
        if self.software == 'tasmota' and self.build_flags != '':
            self.build_flags = '\n'.join(['#define {}'.format(flag) for flag in self.build_flags])
        elif self.software == 'custom-mqtt-programs':
            self.build_flags += ('-DWIFI_SSID=\\"{wifi_ssid}\\" '
                                 '-DWIFI_PASSWORD=\\"{wifi_pass}\\" '
                                 '-DNAME=\\"{name}\\" '
                                 '-DMQTT_SERVER=\\"{mqtt_host}\\" '
                                 '-DTOPIC=\\"{base_topic}/{topic}\\"').format(**self)
        self.mqtt = mqtt.Client(clean_session=True, client_id="espq_{name}".format(**self))
        self.mqtt.reinitialise()
        self.mqtt.on_message = self._on_message
        self.online = False
        self.flashed = False
        self.reported_ip = ''           # IP reported from status 5
        self.reported_mac = ''          # MAC reported from status 5
        self.reported_wificonfig = ''   # WifiConfig reported from status 5
        self.tas_version = ''           # Tasmota version reported from status 2
        self.core_version = ''          # Core version reported from status 2
        self.reported_ssid = ''         # Wi-Fi SSID device is connected to
        self.reported_bssid = ''        # Access point device is connected to
        self.reported_channel = ''      # Wi-Fi channel device is on
        self.reported_rssi = ''         # RSSI of Wi-Fi connection

    def __getattr__(self, name):
        if name in self:
            return self[name]
        else:
            raise AttributeError('No such attribute: ' + name)

    def __setattr__(self, name, value):
            self[name] = value

    def __repr__(self):
        return('\n'.join('{}: {}'.format(k, str(v)) for k, v in self.items() if v != ''))

    def __del__(self):
        self.mqtt.disconnect()

    def _on_message(self, mqtt, userdata, msg):
        print(msg.topic + ' ' + msg.payload)

    def flash(self, flash_mode, serial_port):
        self.flash_mode = flash_mode
        if serial_port != '':
            self.serial_port = serial_port

        # Flash the right software.
        if self.software == 'tasmota':
            self.flashed = self.flash_tasmota()
        else:
            self.flashed = self.flash_custom()
        # If it flashed correctly, watch for it to come online.
        if self.flashed == True:
            print(('{GREEN}{f_name} flashed successfully. Waiting for it to '
                   'come back online...{NOCOLOR}'.format(**colors, **self)))
            sleep(1)
            self.online_check()
        else:
            self._handle_result(1)

        if self.online == False and self.flashed == True:
            self._handle_result(2)
        # If it came back online, run any setup commands,
        # and watch for it to come online again.
        elif self.flashed == True and self.online == True:
            # Skip commands if there are none.
            if not hasattr(self, 'commands') or self.commands is None or not self.commands:
                self._handle_result(0)
                return(0)
            else:
                print(('{GREEN}{f_name} is online after flashing. Running '
                       'setup commands...{NOCOLOR}'.format(**colors, **self)))
                sleep(1)
                self.online = False
                self.run_backlog_commands()
                sleep(1)
                self.online_check()

            if self.online == True:
                self._handle_result(0)
                return(0)
            else:
                self._handle_result(3)
        return(1)

    def _handle_result(self, result_code):
        if result_code == 0:
            result = ('{time}\t{GREEN}{f_name} flashed successfully.'
                      '{NOCOLOR}').format(time=str(datetime.datetime.now()),
                                          **colors,
                                          **self)
            with open(espqdir + "/flash_success.log", "a+") as flashlog:
                flashlog.write(result)
                print(result)
            return()
        elif result_code == 1:
            result = ('{time}\t{RED}{f_name}{NOCOLOR} failed '
                     'to flash.').format(time=str(datetime.datetime.now()),
                                        **colors,
                                        **self)
        elif result_code == 2:
            result = ('{time}\t{RED}{f_name} did not come back online after'
                      'flashing.{NOCOLOR}'.format(time=str(datetime.datetime.now()),
                                                 **colors,
                                                 **self))
        elif result_code == 3:
            result = ('{time}\t{RED}{f_name} did not come back online after '
                      'setup commands.{NOCOLOR}'.format(time=str(datetime.datetime.now()),
                                                  **colors,
                                                  **self))
        else:
            result = ('{time}\t{RED}{f_name} WTF? Shit\'s broken.'
                      '{NOCOLOR}').format(time=str(datetime.datetime.now()),
                                               **colors,
                                               **self)
        with open(espqdir + "/flash_error.log", "a+") as errorlog:
            errorlog.write(result)
            print('{RED}{result}{NOCOLOR}'.format(**colors, result=result))
        return()

    def write_tasmota_config(self):
        """
            Fills tasmota device parameters into blank_defines.h and writes it
            to {tasmotadir}/sonoff/user_config_override.h
        """
        with open(blank_defines, 'r') as f:
            defines = f.read()
        # Every device attribute is passed to the string formatter
        defines = defines.format(**self, datetime=datetime.datetime.now(),
                                 cfg_holder=str(randint(1, 32000)))
        # Write the config file
        with open(os.path.join(tasmotadir, 'sonoff', 'user_config_override.h'), 'w') as f:
            f.write(defines)

    def flash_tasmota(self):
        """ Flash the device with tasmota """
        # Make sure device is tasmota
        if self.software != 'tasmota':
            print('{f_name} is {software}, not tasmota'.format(**self))
            return(False)
        if current_tasmota_version != get_tasmota_version():
            print('{RED}Error: Tasmota version mismatch{NOCOLOR}'.format(**colors))
            return(False)
        self.write_tasmota_config()

        correctPIO = os.path.join(espqdir, 'platformio.ini')
        tasmotaPIO = os.path.join(tasmotadir, 'platformio.ini')
        if filecmp.cmp(correctPIO, tasmotaPIO) == False:
            shutil.copyfile(correctPIO, tasmotaPIO)

        os.chdir(tasmotadir)
        pio_call = 'platformio run -e {environment} -t upload --upload-port {port}'
        if self.flash_mode == 'wifi':
            pio_call = pio_call.format(environment='sonoff-wifi', port=(self.ip_addr + '/u2'))
            print(('{BLUE}Now flashing {module} {f_name} with {software} via '
                '{flash_mode} at {ip_addr}{NOCOLOR}'.format(**colors, **self)))
        elif self.flash_mode == 'serial':
            pio_call = pio_call.format(environment='sonoff-serial', port=self.serial_port)
            print(('{BLUE}Now flashing {module} {f_name} with {software} via '
                '{flash_mode} at {serial_port}{NOCOLOR}'.format(**colors, **self)))
        print('{BLUE}{f_name}\'s MQTT topic is {base_topic}/{topic}{NOCOLOR}'.format(**colors, **self))
        print(pio_call)
        flash_result = call(pio_call, shell=True)
        os.chdir(espqdir)
        return(True if flash_result == 0 else False)

    def flash_custom(self):
        """ Flash the device with custom firmware """
        src_dir = os.path.join(custom_dir, self.module)
        os.environ['PLATFORMIO_SRC_DIR']=src_dir
        os.environ['PLATFORMIO_BUILD_FLAGS']=self.build_flags
        print(os.environ['PLATFORMIO_SRC_DIR'])
        print(os.environ['PLATFORMIO_BUILD_FLAGS'])
        os.chdir(custom_dir)
        pio_call = 'platformio run -e {board}-{flash_mode} -t upload --upload-port {port}'
        if self.flash_mode == 'wifi':
            pio_call = pio_call.format(port=self.ip_addr, **self)
            print(('{BLUE}Now flashing {module} {f_name} in {software} via '
                '{flash_mode} at {ip_addr}{NOCOLOR}'.format(**colors, **self)))
        elif self.flash_mode == 'serial':
            pio_call = pio_call.format(port=self.serial_port, **self)
            print(('{BLUE}Now flashing {module} {f_name} in {software} via '
                '{flash_mode} at {serial_port}{NOCOLOR}'.format(**colors, **self)))
        else:
            print("no flash mode set, try again?")
            return(False)

        print(pio_call)
        flash_result = call(pio_call, shell=True)

        os.environ['PLATFORMIO_SRC_DIR']=''
        os.environ['PLATFORMIO_BUILD_FLAGS']=''
        os.chdir(espqdir)
        return(True if flash_result == 0 else False)

    def online_check(self):
        """
            Connect to MQTT and watch for the device to publish LWT Online
            Returns True if device is Online
        """
        self.online = False
        online_topic = '{t_topic}/INFO2'.format(**self)
        print('{BLUE}Watching for {}{NOCOLOR}'.format(online_topic, **colors))
        self.mqtt.connect(self.mqtt_host)
        self.mqtt.message_callback_add(online_topic, lambda *args: setattr(self, 'online', True))
        self.mqtt.subscribe(online_topic)
        starttime = datetime.datetime.now()
        while self.online == False and (datetime.datetime.now() - starttime).total_seconds() < wait_time:
            self.mqtt.loop(timeout=loop_time)
        time_waited = (datetime.datetime.now() - starttime).total_seconds()
        if self.online == False:
            print('{RED}{f_name} did not come online within {wait_time} '
                  'seconds{NOCOLOR}'.format(f_name=self.f_name, wait_time=str(wait_time), **colors))
        elif self.online == True:
            print('{GREEN}{f_name} came online in {time_waited} '
                  'seconds{NOCOLOR}'.format(f_name=self.f_name, time_waited=time_waited, **colors))
        self.mqtt.unsubscribe(online_topic)
        self.mqtt.message_callback_remove(online_topic)
        self.mqtt.disconnect()

    def run_backlog_commands(self):
        """ Issue setup commands for tasmota over MQTT with backlog """
        if not hasattr(self, 'commands') or self.commands == '':
            print('{BLUE}No commands for {f_name}, skipping.{NOCOLOR}'.format(**colors, **self))
        else:
            self.mqtt.connect(self.mqtt_host)
            backlog_topic = '{c_topic}/backlog'.format(**self)
            # Join all command/payload pairs together with semicolons. If the
            # payload is a tasmota GPIO, use the value of the enumeration.
            backlog_payload = '; '.join(['{c} {p}'.format(c=i['command'], p=get_gpio(i['payload']) if 'GPIO' in i['payload'] else i['payload']) for i in self.commands]) + '; restart 1'
            print('{BLUE}Sending {topic} {payload}{NOCOLOR}'.format(topic=backlog_topic, payload=backlog_payload, **colors))
            self.mqtt.publish(backlog_topic, backlog_payload)
            self.mqtt.disconnect()

    def query_tas_status(self, attributes):
        """
            Query a tasmota device's status of a certain type.
            Attributes can be wifi_attr, network_attr, or firmware_attr.
        """
        def _tas_status_callback(mqtt, userdata, msg):
            for k, v in attributes['values'].items():
                setattr(self, k, nested_get(literal_eval(msg.payload.decode('UTF-8')), v))
        s_topic = '{s_topic}/{stat_topic}'.format(stat_topic=attributes['stat_topic'], **self)
        c_topic = '{c_topic}/status'.format(**self)
        self.mqtt.message_callback_add(s_topic, _tas_status_callback)
        self.mqtt.connect(self.mqtt_host)
        self.mqtt.subscribe(s_topic)
        starttime = datetime.datetime.now()
        self.mqtt.publish(c_topic, attributes['status_payload'])
        # check and see if the last attribute has been found yet
        while getattr(self, list(attributes['values'].keys())[-1]) == '' and (datetime.datetime.now() - starttime).total_seconds() < loop_time:
            self.mqtt.loop(timeout=loop_time)
        self.mqtt.unsubscribe(s_topic)
        self.mqtt.message_callback_remove(s_topic)
        self.mqtt.disconnect()

    def write_hass_config(self):
        """
            Write yaml config files for Home Assistant.
            Templates should go in hass_template_dir, and
            outputs will go into hass_output_dir/{component_type}/
            (i.e. a sonoff basic on a light has the standard light component,
            plus many sensor components for its metadata)
        """
        if not os.path.isdir(hass_output_dir):
            os.mkdir(hass_output_dir)
        # Add the home assistant template dir to path and import the device's domain
        try:
            sys.path.append(hass_template_dir)
            self.yaml_template = __import__(self.hass_template)
        except:
            print('Device {f_name} domain error, cannot write hass config.'.format(**self))
            return()
        # Go through all types of components for domain
        # (i.e. domain light has components light & sensor)
        for c in self.yaml_template.components:
            if not os.path.isdir(os.path.join(hass_output_dir, c)):
                os.mkdir(os.path.join(hass_output_dir, c))
            with open('{dir}/{c}/{name}_{c}.yaml'.format(dir=hass_output_dir, c = c, **self), 'w') as yamlf:
                yamlf.write(self.yaml_template.components[c].format(**self))


def import_devices(device_file):
    """
        Process JSON file of device configs, export a list of device objects.
        Keyword arguments:
        device_file -- JSON file of devices
    """
    with open(device_file, 'r') as f:
        device_import = json.load(f)
    devices=[]
    for dev in device_import:
        try:
            for id_info in dev['ids']:
                new_dev = deepcopy(dev)
                for key in new_dev:
                    if isinstance(new_dev[key], str):
                        new_dev[key] = sub('%id%',
                                           id_info['id'],
                                           new_dev[key])
                new_dev['ip_addr'] = id_info['ip_addr']
                if 'mac_addr' in id_info:
                    new_dev['mac_addr'] = id_info['mac_addr']
                # Need to add processing of custom device parameters
                devices.append(device(new_dev))
        except KeyError:
            devices.append(device(dev))
    devices = sorted(devices, key=lambda k: k.module)
    return devices

def nested_get(input_dict, nested_key):
    """
        Retrieve the value of a nested dictionary key given the nested
        dictionary (input_dict) and the nested keys as a list (nested_key)
    """
    internal_dict_value = input_dict
    for k in nested_key:
        internal_dict_value = internal_dict_value.get(k, None)
        if internal_dict_value is None:
            return(None)
    return(internal_dict_value)

def get_gpio(request):
    """ Retrieve a GPIO's integer value from the enumeration in tasmota """
    lines=[]
    append=False
    with open(tasmotadir + "/sonoff/sonoff_template.h", "r") as f:
        for line in f:
            if append==True:
                split = line.split('//')[0]
                subbed = sub('[\\s+,;}]','', split)
                lines.append(subbed)
            if 'UserSelectablePins' in line:
                append=True
            if '}' in line:
                append=False
    gpios={}
    for num, gpio in enumerate(lines):
        gpios[gpio] = num
    return(gpios[request])

def get_tasmota_version():
    """ Retrieve a GPIO's integer value from the enumeration in tasmota """
    matches = []
    with open(tasmotadir + "/sonoff/sonoff_version.h", "r") as f:
        for line in f:
            matches += findall('0x\d+', line)
    if len(matches) == 0:
        raise Exception('No tasmota version found.')
    elif len(matches) == 1:
        return matches[0]
    else:
        raise IndexError('Too many tasmota versions found.')

if __name__ == "__main__":
    print("Don't call espq directly")
    exit(1)
