"""
Driver for Aceinna OpenIMU
Based on PySerial https://github.com/pyserial/pyserial
Created on 2018-06-17
@author: m5horton
"""

"""
WS Master Connection 
connect         - finds device, gets device_id/odr_setting, and loops
                - run this in thread otherwise blocking
disconnect      - ends loop

Device Discovery
find_device     - entry point to find a serial connected IMU
find_ports
autobaud

Logging
start_log
stop_log

Syncing 
sync            - syncs to a unit continuously transmitting, or a specific packet response

Data Functions

Serial          - a tiny layer on top of Pyserial to handle exceptions as means of device detection
open
close
read
write
reset_buffer
"""
import serial
import math
import string
import time
import sys
import file_storage
import collections
import glob
import struct
import json
from imu_input_packet import InputPacket
from bootloader_input_packet import BootloaderInputPacket

class OpenIMU:
    def __init__(self, ws=False):
        '''Initialize and then start ports search and autobaud process
        '''
        self.ws = ws                # set to true if being run as a thread in a websocket server
        self.ser = None             # the active UART
        self.synced = 0             # synced status in streaming mode
        self.stream_mode = 0        # 0 = polled, 1 = streaming, commanded by set_quiet and restore_odr
        self.device_id = 0          # unit's id str
        self.connected = 0          # imu is successfully connected to a com port, kind of redundant with device_id property
        self.odr_setting = 0        # value of the output data rate EEPROM setting
        self.logging = 0            # logging on or off
        self.logger = None          # the file logger instance
        self.packet_size = 0        # expected size of packet 
        self.packet_type = 0        # expected type of packet
        self.elapsed_time_sec = 0   # an accurate estimate of elapsed time in ODR mode using IMU timer data
        self.data = {}              # placeholder imu measurements of last converted packeted
        self.response_data = {}     # placeholder for imu command response data
        with open('openimu.json') as json_data:
            self.imu_properties = json.load(json_data)

    def find_device(self):
        ''' Finds active ports and then autobauds units
        '''
        while not self.autobaud(self.find_ports()):
            time.sleep(0.05)
        return True
        
    def find_ports(self):
        ''' Lists serial port names. Code from
            https://stackoverflow.com/questions/12090503/listing-available-com-ports-with-python
            Successfully tested on Windows 8.1 x64, Windows 10 x64, Mac OS X 10.9.x / 10.10.x / 10.11.x and Ubuntu 14.04 / 14.10 / 15.04 / 15.10 with both Python 2 and Python 3.
            :raises EnvironmentError:
                On unsupported or unknown platforms
            :returns:
                A list of the serial ports available on the system
        '''
        print('scanning ports')
        if sys.platform.startswith('win'):
            ports = ['COM%s' % (i + 1) for i in range(256)]
        elif sys.platform.startswith('linux') or sys.platform.startswith('cygwin'):
            # this excludes your current terminal "/dev/tty"
            ports = glob.glob('/dev/tty[A-Za-z]*')
        elif sys.platform.startswith('darwin'):
            ports = glob.glob('/dev/tty.*')
        else:
            raise EnvironmentError('Unsupported platform')

        result = []
        for port in ports:
            if "Bluetooth" in port:
                continue
            try:
                #print('Trying: ' + port)
                s = serial.Serial(port)
                s.close()
                result.append(port)
            except (OSError, serial.SerialException):
                pass
        return result

    def autobaud(self, ports):
        '''Autobauds unit - first check for stream_mode / continuous data, then check by polling unit
           :returns: 
                true when successful
        ''' 
        for port in ports:
            self.open(port)
            # TODO: change this to intelligently use openimu.json.  even save the last configuration 
            for baud in [57600, 115200]:
                self.ser.baudrate = baud
                self.device_id = self.openimu_get_device_id()
                if (sys.version_info > (3, 0)) and bytes("Bootloader", 'utf-8') in self.device_id \
                    or (sys.version_info < (3, 0)) and  "Bootloader" in self.device_id:
                    print('BOOTLOADER MODE') 
                    print('Connected ....{0}'.format(self.device_id))
                    print('Please Upgrade FW with upgrade_fw function')
                    return True
                elif self.device_id:
                    print('Connected ....{0}'.format(self.device_id))
                    packet_rate = next((x for x in self.imu_properties['userConfiguration'] if x['name'] == 'Packet Rate'), None)
                    odr_param = self.openimu_get_param(packet_rate['paramId'])
                    self.odr_setting = odr_param['value']
                    if self.odr_setting:
                        self.stream_mode = 1
                    return True
        return False
        
    def get_latest(self):
        '''Get latest converted IMU readings in converted units
            :returns:
                data object or error message for web socket server to pass to app
        '''
        if self.stream_mode == 1:
            return self.data
        else: 
            return { 'error' : 'not streaming' }
    
    def start_log(self, data = False):
        '''Creates file or cloud logger.  Autostarts log activity if ws (websocket) set to false
        '''
        if self.ws == False: 
            if not self.device_id:
                self.find_device()
            if self.odr_setting:
                print('start logging')
                self.logger = file_storage.OpenIMULog(self,data)
                self.logging = 1
                self.connect()
        elif self.connected and self.odr_setting:
            self.logger = file_storage.OpenIMULog(self,data)
            self.logging = 1
                
    def stop_log(self):
        '''Stops file or cloud logger
        '''
        self.logging = 0
        self.logger.close()
        self.logger = None

    def openimu_update_param(self, param, value):
        C = InputPacket(self.imu_properties, 'uP', param, value)
        self.write(C.bytes)
        data = self.sync(sync_type='uP')
        return self.response_data

    def openimu_get_param(self, param,):
        C = InputPacket(self.imu_properties, 'gP', param)
        self.write(C.bytes)
        self.sync(sync_type='gP')
        return self.response_data

    def openimu_save_config(self):
        C = InputPacket(self.imu_properties, 'sC')
        self.write(C.bytes)
        # this message currently does not return anything

    def openimu_get_all_param(self):
        C = InputPacket(self.imu_properties, 'gA')
        self.write(C.bytes)
        self.synced = 0
        self.sync(sync_type='gA')
        return self.response_data  

    def openimu_get_device_id(self):
        C = InputPacket(self.imu_properties, 'pG')
        self.write(C.bytes)
        time.sleep(0.05)
        self.synced = 0
        self.sync(sync_type='pG')
        return self.response_data 

    def openimu_get_user_app_id(self): 
        C = InputPacket(self.imu_properties, 'gV')
        self.write(C.bytes)
        self.synced = 0
        self.sync(sync_type='gV')
        return self.response_data    

    def connect(self):
        '''Continous data collection loop to get and process data packets
        '''
        if not self.device_id:
            self.find_device()
        self.connected = 1
        while self.odr_setting and self.connected:
            if self.stream_mode:
                self.get_packet()
            elif self.ws:
                time.sleep(0.05)
      
    def disconnect(self):
        '''Ends data collection loop.  Reset settings
        '''
        self.data = {}
        self.response_data = {}
        self.connected = 0
        self.device_id = 0
        self.odr_setting = 0
        self.stream_mode = 0
        self.synced = 0
        self.packet_size = 0        
        self.packet_type = 0        
      
    def get_packet(self):
        '''Syncs unit and gets packet.  Assumes unit is in stream_mode'''

        # Already synced
        if self.synced == 1:    
            # Read next packet of data based on expected packet size     
            S = self.read(self.packet_size + 7)
            
            if len(S) < 2:
                # Read Failed
                self.synced = 0                    
                return
            if S[0] == 85 and S[1] == 85:
                packet_crc = 256 * S[-2] + S[-1]    
                # Compare computed and read crc               
                if self.calc_crc(S[2:S[4]+5]) == packet_crc: 
                    # 5 is offset of first payload byte, S[4]+5 is offset of last payload byte     
                    self.data = self.parse_packet(S[5:S[4]+5]) 
                else:
                    self.synced = 0
                    return    
            else: 
                # Get synced and then read next packet
                self.sync()
                self.get_packet()
        else:
            # Get synced and then read next packet
            self.sync()
            self.get_packet()

    def sync(self,prev_byte = 0,bytes_read = 0, sync_type = None):
        '''
            syncs to the packet stream, or finds the next command response message of type sync_type
        '''
        S = self.read(1)
        data = []

        if not S:
            return False
        if S[0] == 85 and prev_byte == 85:      # VALID HEADER FOUND
            # Once header is found then read off the rest of packet and check CRC
            config_bytes = self.read(3)
            self.packet_type = '{0:1c}'.format(config_bytes[0]) + '{0:1c}'.format(config_bytes[1])
            self.packet_size = config_bytes[2]
            S = self.read(config_bytes[2] + 2)      # clear bytes off port, payload + 2 byte CRC
            S = config_bytes + S                    # reform data for CRC check
            # adding this here 6/13
            packet_crc = 256 * S[-2] + S[-1]    
            # Compare computed and read crc excluding head and CRC itself       
            if self.calc_crc(S[:S[2]+3]) == packet_crc: 
                # 2 is offset of first payload byte, S[2]+3 is offset of last payload byte     
                data = self.parse_packet(S[3:S[2]+3]) 
            else:
                # Start over
                self.sync(sync_type=sync_type)
            
            
            if not sync_type: 
                self.stream_mode = 1
                self.synced = 1
                self.data = data
                return True
            elif sync_type and sync_type != self.packet_type:
                self.sync(0,0,sync_type)
            elif sync_type == self.packet_type:
                #returns data for a response packed type
                self.response_data = data
                return True
           
        else: 
            # Repeat sync to search next byte pair for header
            # Reads up to 300 bytes in search of a packet.  
            bytes_read = bytes_read + 1
            self.synced = 0
            if (bytes_read < 300):
                self.sync(S[0], bytes_read, sync_type)
            else:
                # Unit is quiet
                return False
      
    def parse_packet(self, payload, ws = False):
        '''Parses packet using openimu.json as the key
        '''
        output_packet = next((x for x in self.imu_properties['userMessages']['outputPackets'] if x['name'] == self.packet_type), None)
        input_packet = next((x for x in self.imu_properties['userMessages']['inputPackets'] if x['name'] == self.packet_type), None)
        bootloader_packet = next((x for x in self.imu_properties['bootloaderMessages'] if x['name'] == self.packet_type), None)
        
        if output_packet != None:
            data = self.openimu_unpack_output_packet(output_packet, payload)

            if self.logging == 1 and self.logger is not None:
                self.logger.log(data, self.odr_setting) 
            
            return data

        elif input_packet != None:
            
            data = self.openimu_unpack_input_packet(input_packet['responsePayload'], payload)
            return data

        elif bootloader_packet != None:

            data = self.openimu_unpack_bootloader_packet(bootloader_packet['responsePayload'], payload)
            return data

    def calc_crc(self,payload):
        '''Calculates CRC per 380 manual
        '''
        crc = 0x1D0F
        for bytedata in payload:
           crc = crc^(bytedata << 8) 
           for i in range(0,8):
                if crc & 0x8000:
                    crc = (crc << 1)^0x1021
                else:
                    crc = crc << 1

        crc = crc & 0xffff
        return crc

    def open(self, port, baud = 57600):
        try:
            self.ser = serial.Serial(port, baud, timeout = 0.1)
        except (OSError, serial.SerialException):
            print('serial port open exception' + port)

    def close(self):
            self.ser.reset_input_buffer()
            self.ser.close()
    
    def read(self,n):
        bytes = []
        try: 
            bytes = self.ser.read(n)
        #except:
        except (OSError, serial.SerialException):
            self.disconnect()    # sets connected to 0, and other related parameters to initial values
            print('serial exception read') 
            self.connect() 
        if bytes and len(bytes):
            return bytearray(bytes)
        else:
            print('empty read') 
            return bytearray(bytes)
    
    def write(self,n):
        try: 
            self.ser.write(n)
        except:
        # except (OSError, serial.SerialException):
            self.disconnect()   # sets connected to 0, and other related parameters to initial values  
            print('serial exception write')
            self.connect() 

    def reset_buffer(self):
        try:
            self.ser.reset_input_buffer()
        except:
        #except (OSError, serial.SerialException):
            self.disconnect()   # sets connected to 0, and other related parameters to initial values
            print('serial exception reset')   
            self.connect() 

    def openimu_unpack_output_packet(self, output_message, payload):
        length = 0
        pack_fmt = '<'
        for value in output_message['payload']:
            if value['type'] == 'float':
                pack_fmt += 'f'
                length += 4
            elif value['type'] == 'uint32':
                pack_fmt += 'I'
                length += 4
            elif value['type'] == 'int32':
                pack_fmt += 'i'
                length += 4
            elif value['type'] == 'int16':
                pack_fmt += 'h'
                length += 2
            elif value['type'] == 'uint16':
                pack_fmt += 'H'
                length += 2
            elif value['type'] == 'double':
                pack_fmt += 'd'
                length += 8
            elif value['type'] == 'int64':
                pack_fmt += 'q'
                length += 8
            elif value['type'] == 'uint64':
                pack_fmt += 'Q'
                length += 8
            elif value['type'] == 'char':
                pack_fmt += 'c'
                length += 1
            elif value['type'] == 'uchar':
                pack_fmt += 'B'
                length += 1
        len_fmt = '{0}B'.format(length)
        b = struct.pack(len_fmt, *payload)
        data = struct.unpack(pack_fmt, b)
        out = [(value['name'],data[idx]) for idx,value in enumerate(output_message['payload'])]
        data = collections.OrderedDict(out)
        return data
    
    def openimu_unpack_input_packet(self, input_message, payload):
        if input_message['type'] == 'userConfiguration':
            user_configuration = self.imu_properties['userConfiguration']
            params = []
            for parameter in user_configuration:
                id = parameter['paramId']
                type = parameter['type']
                name = parameter['name']
                value = self.openimu_unpack_one(type, payload[id*8:(id+1)*8])
                print('{0}: {1}'.format(name,value))
                params.append({ "paramId": id, "name": name, "value": value})
            return params
        elif input_message['type'] == 'userParameter':
            user_configuration = self.imu_properties['userConfiguration']
            param_id = self.openimu_unpack_one('uint32', payload[0:4]) 
            param = user_configuration[param_id]
            param_value = self.openimu_unpack_one(param['type'], payload[4:12])
            print('{0}: {1}'.format(param['name'], param_value))
            return { "paramId": param_id, "name": param['name'], "value": param_value }
        elif input_message['type'] == 'paramId':
            user_configuration = self.imu_properties['userConfiguration']
            error = self.openimu_unpack_one('uint32', payload[0:4]) 
            if not error:
                print("Successfully Updated")
            return { "error": error }
        elif input_message['type'] == 'string':
            return payload

    def openimu_unpack_bootloader_packet(self, bootloader_message, payload):
        if bootloader_message['type'] == 'ack':
            print('Success')
            return { "error": 0 }

    def openimu_unpack_one(self, type, data):
        if type == 'uint64':
            b = struct.pack('8B', *data)
            return struct.unpack('<Q', b)[0]
        elif type == 'int64':
            b = struct.pack('8B', *data)
            return struct.unpack('<q', b)[0]
        elif type == 'uint32':
            b = struct.pack('4B', *data)
            return struct.unpack('<L', b)[0]
        elif type == 'char8':
            return struct.pack('8B', *data)

 #####

    def openimu_start_bootloader(self):
        packet = BootloaderInputPacket(self.imu_properties, 'JI')
        self.write(packet.bytes)
        self.sync(sync_type='JI')
        self.ser.baudrate = 57600   # Force baud rate to 57600
        bootloader_id = self.openimu_get_device_id()
        print(bootloader_id)
        return True
    
        
    def openimu_start_app(self):
        '''Starts app
        '''
        packet = BootloaderInputPacket(self.imu_properties, 'JA')
        self.write(packet.bytes)
        print('Restarting app ...')
        time.sleep(5)
        self.disconnect()
        self.reset_buffer()
        self.close()
        self.find_device()      # Must go thru reconnect process because baud rate may have changed during firmware load
        return True

    def openimu_write_block(self, data_len, addr, data):
        print(data_len, addr)
        packet = BootloaderInputPacket(self.imu_properties, 'WA', data_len, addr, data)
        self.write(packet.bytes)
        if addr == 0:
            time.sleep(5)
        self.sync(sync_type='WA')
         
    def openimu_upgrade_fw(self,file):
        '''Upgrades firmware of connected 380 device to file provided in argument
        '''
        if not self.openimu_start_bootloader():
            print('Bootloader Start Failed')
            return False

        print('upgrade fw')
        max_data_len = 240
        addr = 0
        fw = open(file, 'rb').read()
        fs_len = len(fw)
        
        while (addr < fs_len):
            packet_data_len = max_data_len if (fs_len - addr) > max_data_len else (fs_len-addr)
            data = fw[addr:(addr+packet_data_len)]
            self.openimu_write_block(packet_data_len, addr, data)
            addr += packet_data_len
        # Start new app
        self.openimu_start_app()



 #####       

if __name__ == "__main__":
    grab = OpenIMU()
    grab.find_device()
    #grab.openimu_update_param(4,10)
    #grab.openimu_save_config()
    data = grab.openimu_get_all_param()
    print(data)
    #grab.openimu_update_param(3,'zT')
    #user_fw_id = grab.openimu_get_user_app_id()
    #print(user_fw_id)
    #grab.openimu_upgrade_fw('firmware44.bin')
    #user_fw_id = grab.openimu_get_user_app_id()
    #print(user_fw_id)
    #grab.openimu_update_param(0,0)
    #grab.openimu_update_param(1,0)
    #grab.openimu_update_param(2,115200)
    #grab.openimu_update_param(3,'z2')
    grab.openimu_update_param(4,0)
    grab.openimu_save_config()
    #grab.openimu_update_param(5,20)
    #grab.openimu_update_param(6,25)
    #for i in range(0,6):
    #    grab.openimu_get_param(i)
    #grab.openimu_get_all_param()
	#grab.openimu_save_config()
    #grab.upgrade_fw('MTLT305D_19.0.6.bin')
	# Examples for logging
    #time.sleep(2)
    #grab.start_log()
    #time.sleep(10)
    #grab.stop_log()
    # Test for WS Server
    #grab.read_fields([0x0001, 0x0002, 0x0003])
    #grab.get_fields([0x0001, 0x0002, 0x0003])
    #grab.write_fields([[3, 21296]])
    #grab.read_fields([0x0001, 0x0002, 0x0003])
    #grab.get_fields([0x0001, 0x0002, 0x0003])
    
