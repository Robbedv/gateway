# Copyright (C) 2016 OpenMotics BV
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
Tool to bootload the power modules from the command line.
"""
from platform_utils import System
System.import_eggs()

import intelhex
import constants
import sys
import argparse
import logging
import time
from ioc import Injectable
from ConfigParser import ConfigParser
from serial import Serial
from serial_utils import RS485, CommunicationTimedOutException
from power.power_communicator import PowerCommunicator
from power.power_controller import PowerController
from power.power_api import bootloader_goto, bootloader_read_id, bootloader_write_code, \
                            bootloader_jump_application, bootloader_erase_code, get_version, \
                            read_eeprom, write_eeprom, \
                            POWER_API_8_PORTS, POWER_API_12_PORTS

logger = logging.getLogger("openmotics")


def setup_logger():
    """ Setup the OpenMotics logger. """
    logger.setLevel(logging.INFO)
    logger.propagate = False

    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)

    handler = logging.FileHandler(constants.get_update_log_location(), mode='w')
    handler.setLevel(logging.INFO)
    handler.setFormatter(logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
    logger.addHandler(handler)


class HexReader(object):
    """ Reads the hex from file and returns it in the OpenMotics format. """

    def __init__(self, hex_file):
        """ Constructor with the name of the hex file. """
        self.__hex = intelhex.IntelHex(hex_file)
        self.__crc = 0

    def get_bytes_version_8(self, address):
        """ Get the 192 bytes from the hex file, with 3 address bytes prepended. """
        data_bytes = [address % 256,
                      (address % 65536) / 256,
                      address / 65536]

        iaddress = address * 2
        for i in range(64):
            data0 = self.__hex[iaddress + (4 * i) + 0]
            data1 = self.__hex[iaddress + (4 * i) + 1]
            data2 = self.__hex[iaddress + (4 * i) + 2]

            if address == 0 and i == 0:  # Set the start address to the bootloader: 0x400
                data1 = 4

            data_bytes.append(data0)
            data_bytes.append(data1)
            data_bytes.append(data2)

            if not (address == 43904 and i >= 62):  # Don't include the CRC bytes in the CRC
                self.__crc += data0 + data1 + data2

        if address == 43904:  # Add the CRC at the end of the program
            data_bytes[-1] = self.__crc % 256
            data_bytes[-2] = (self.__crc % (256 * 256)) / 256
            data_bytes[-3] = (self.__crc % (256 * 256 * 256)) / (256 * 256)
            data_bytes[-4] = (self.__crc % (256 * 256 * 256 * 256)) / (256 * 256 * 256)

        return data_bytes

    @staticmethod
    def int_to_array_12(integer):
        """ Convert an integer to an array for the 12 port energy module. """
        return [integer % 256, (integer % 65536) / 256, (integer / 65536) % 256, (integer / 65536) / 256]

    def get_bytes_version_12(self, address):
        """ Get the 128 bytes from the hex file, with 4 address bytes prepended. """
        data_bytes = self.int_to_array_12(address)

        for i in range(32):
            data0 = self.__hex[address + (4 * i) + 0]
            data1 = self.__hex[address + (4 * i) + 1]
            data2 = self.__hex[address + (4 * i) + 2]
            data3 = self.__hex[address + (4 * i) + 3]

            data_bytes.append(data0)
            data_bytes.append(data1)
            data_bytes.append(data2)
            data_bytes.append(data3)

            if not (address == 486801280 and i == 31):
                self.__crc += data0 + data1 + data2 + data3

        if address == 486801280:
            data_bytes = data_bytes[:-4]
            data_bytes += self.int_to_array_12(self.get_crc())

        return data_bytes

    def get_crc(self):
        """ Get the crc for the block that have been read from the HexReader. """
        return self.__crc


def get_module_firmware_version(module_address, power_communicator):
    """
    Get the version of a power module.

    :param module_address: The address of a power module (integer).
    :param power_communicator: Communication with the power modules.
    """
    raw_version = power_communicator.do_command(module_address, get_version())[0]
    cleaned_version = raw_version.split('\x00', 1)[0]
    parsed_version = cleaned_version.split('_')
    if len(parsed_version) != 4:
        return cleaned_version
    return '{0}.{1}.{2} ({3})'.format(parsed_version[1], parsed_version[2], parsed_version[3], parsed_version[0])


def bootload_8(module_address, hex_file, power_communicator):
    """
    Bootload a 8 port power module.

    :param module_address: The address of a power module (integer).
    :param hex_file: The filename of the hex file to write.
    :param power_communicator: Communication with the power modules.
    """
    logger.info('E{0} - Version: {0}'.format(module_address, get_module_firmware_version(module_address, power_communicator)))
    logger.info('E{0} - Start bootloading'.format(module_address))
    reader = HexReader(hex_file)

    logger.info('E{0} - Going to bootloader'.format(module_address))
    power_communicator.do_command(module_address, bootloader_goto(), 10)

    logger.info('E{0} - Reading chip id'.format(module_address))
    chip_id = power_communicator.do_command(module_address, bootloader_read_id())
    if chip_id[0] != 213:
        raise Exception('Unknown chip id: {0}'.format(chip_id[0]))

    logger.info('E{0} - Writing vector tabel'.format(module_address))
    for address in range(0, 1024, 128):      # 0x000 - 0x400
        data = reader.get_bytes_version_8(address)
        power_communicator.do_command(module_address, bootloader_write_code(POWER_API_8_PORTS), *data)

    logger.info('E{0} -  Writing code'.format(module_address))
    for address in range(8192, 44032, 128):  # 0x2000 - 0xAC00
        data = reader.get_bytes_version_8(address)
        power_communicator.do_command(module_address, bootloader_write_code(POWER_API_8_PORTS), *data)

    logger.info('E{0} - Jumping to application'.format(module_address))
    power_communicator.do_command(module_address, bootloader_jump_application())

    logger.info('E{0} - Done'.format(module_address))


def bootload_12(module_address, hex_file, power_communicator):
    """
    Bootload a 12 port power module.

    :param module_address: The address of a power module (integer).
    :param hex_file: The filename of the hex file to write.
    :param power_communicator: Communication with the power modules.
    """
    logger.info('E{0} - Version: {1}'.format(module_address, get_module_firmware_version(module_address, power_communicator)))
    logger.info('E{0} - Start bootloading'.format(module_address))

    try:
        logger.info('E{0} - Reading calibration data'.format(module_address))
        calibration_data = list(power_communicator.do_command(module_address, read_eeprom(12, 100), *[256, 100]))
        logger.info('E{0} - Calibration data: {1}'.format(module_address, ','.join([str(d) for d in calibration_data])))
    except Exception as ex:
        logger.info('E{0} - Could not read calibration data: {1}'.format(module_address, ex))
        calibration_data = None

    reader = HexReader(hex_file)

    logger.info('E{0} - Going to bootloader'.format(module_address))
    power_communicator.do_command(module_address, bootloader_goto(), 10)

    try:
        logger.info('E{0} - Erasing code...'.format(module_address))
        for page in range(6, 64):
            power_communicator.do_command(module_address, bootloader_erase_code(), page)

        logger.info('E{0} - Writing code...'.format(module_address))
        for address in range(0x1D006000, 0x1D03FFFB, 128):
            data = reader.get_bytes_version_12(address)
            power_communicator.do_command(module_address, bootloader_write_code(POWER_API_12_PORTS), *data)
    finally:
        logger.info('E{0} - Jumping to application'.format(module_address))
        power_communicator.do_command(module_address, bootloader_jump_application())

    if calibration_data is not None:
        time.sleep(1)
        logger.info('E{0} - Restoring calibration data'.format(module_address))
        power_communicator.do_command(module_address, write_eeprom(12, 100), *([256] + calibration_data))

    logger.info('E{0} - Done'.format(module_address))


def main():
    """ The main function. """
    logger.info('Energy/Power Module bootloader')
    logger.info('Command: {0}'.format(' '.join(sys.argv)))

    parser = argparse.ArgumentParser(description='Tool to bootload a power module.')
    parser.add_argument('--address', dest='address', type=int,
                        help='the address of the power module to bootload')
    parser.add_argument('--all', dest='all', action='store_true',
                        help='bootload all power modules')
    parser.add_argument('--file', dest='file',
                        help='the filename of the hex file to bootload')
    parser.add_argument('--8', dest='old', action='store_true',
                        help='bootload for the 8-port power modules')
    parser.add_argument('--verbose', dest='verbose', action='store_true',
                        help='show the serial output')

    args = parser.parse_args()

    if not args.file:
        parser.print_help()
        return

    config = ConfigParser()
    config.read(constants.get_config_file())

    port = config.get('OpenMotics', 'power_serial')
    power_serial = RS485(Serial(port, 115200))

    Injectable.value(power_serial=power_serial)
    Injectable.value(power_db=constants.get_power_database_file())

    power_controller = PowerController()
    power_communicator = PowerCommunicator(time_keeper_period=0, verbose=args.verbose)
    power_communicator.start()

    def _bootload(_module, _module_address, filename, is_power_module):
        try:
            if is_power_module and _module['version'] == POWER_API_8_PORTS:
                bootload_8(_module_address, filename, power_communicator)
            elif not is_power_module and _module['version'] == POWER_API_12_PORTS:
                bootload_12(_module_address, filename, power_communicator)
        except CommunicationTimedOutException:
            logger.warning('E{0} - Module unavailable. Skipping...'.format(address))
        except Exception:
            logger.exception('E{0} - Unexpected exception during bootload. Skipping...'.format(address))

    if args.address or args.all:
        power_modules = power_controller.get_power_modules()
        if args.all:
            for module_id in power_modules:
                module = power_modules[module_id]
                address = module['address']
                _bootload(module, address, args.file, is_power_module=args.old)
        else:
            address = args.address
            modules = [module for module in power_modules.values() if module['address'] == address]
            if len(modules) != 1:
                logger.info('ERROR: Cannot find a module with address {0}'.format(address))
                sys.exit(0)
            module = modules[0]
            _bootload(module, address, args.file, is_power_module=args.old)
    else:
        parser.print_help()


if __name__ == '__main__':
    setup_logger()
    main()
