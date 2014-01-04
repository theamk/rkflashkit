#!/usr/bin/env python
# -*- coding: utf-8 -*

import hashlib
import optparse
import rktalk
import sys
import tempfile
import time

class StderrLogger(object):
  # Maximum verification erorrs to print in non-verbose mode.
  MAX_ERRORS = 10

  def __init__(self, stream=None, verbose=2):
    """Create an object. stream is file to write to,
    verbose is verbosity level (0 = quiet, 2 = most verbose)
    """
    if stream is None:
      self.stream = sys.stderr
    else:
      self.stream = stream
    self.verbose = verbose
    self.need_eof = False
    self.num_errors = 0
    self.t0 = time.time()

  def log(self, message, important=False):
    if (not important) and (self.verbose <= 0):
      return

    # Suppress progress messages, replace then with dots.
    if (self.verbose < 2) and (' flash memory at offset ' in message):
      self.need_eof = True
      self.stream.write('.')
      self.stream.flush()
      return

    # Suppress error messages if there are too many of them
    if (self.verbose < 2) and ('is differnt from file' in message):
      self.num_errors += 1
      if self.num_errors == self.MAX_ERRORS:
        message = '\t(rest of errors suppressed)\n'
      elif self.num_errors > self.MAX_ERRORS:
        return

    # Print EOL if we had line of dots before
    if self.need_eof:
      self.stream.write('\n')
      self.need_eof = False

    now = time.strftime('%H:%M:%S ')
    self.stream.write(now + message.replace('\r', '\n').replace('\t', '* '))
    self.stream.flush()

  def print_dividor(self):
    self.log('===================\n')
    self.t0 = time.time()
    self.num_errors = 0

  def print_done(self):
    self.log('\tDone, %.1f seconds\n' % (time.time() - self.t0))

  def print_error(self, message):
    self.log('ERROR: ' + message, important=True)



def cmdline_main(args):
  parser = optparse.OptionParser(
    usage=
    '\n   %prog -l     -- list devices'
    '\n   %prog        -- list partitions'
    '\n   %prog [-p paritition] -ACTION [filename] -- do ACTION (see --help)'
    )


  parser.add_option('-l', '--list-devices', action='store_true',
                    help='Print active devices and exit')
  parser.add_option('-s', '--select', metavar='BUS:DEVNUM',
                    help='Select device to operate on')
  parser.add_option('-v', '--verbose', action='store_true',
                    help='Print more messages')
  parser.add_option('-p', '--part', action='append',
                    help='Partition to work on (may be specified many times)')

  parser.add_option('-M', '--md5', action='store_true',
                   help='Calculate MD5 of partition')
  parser.add_option('-B', '--backup', action='store_true',
                   help='Backup partition to a file (- for stdout)')
  parser.add_option('-E', '--erase', action='store_true',
                   help='Erase partition')
  parser.add_option('-F', '--flash', action='store_true',
                    help='Flash file to partition')
  parser.add_option('-C', '--compare', action='store_true',
                   help='Compare partition to file (highly recommended after '
                    '-F or -B)')
  parser.add_option('-R', '--reboot', action='store_true',
                   help='Reboot device')

  opts, args = parser.parse_args(args)

  if opts.list_devices:
    if args:
      parser.error('Unexpected argument')

    device_uids, device_list = rktalk.list_devices()
    print 'Devices:'
    for bus_id, dev_id, vendor_id, prod_id in device_list:
      dev_name = '0x%04x:0x%04x' % (vendor_id, prod_id)
      print ' Device at %d:%d has type %s' % (bus_id, dev_id, dev_name)
    if len(device_list) == 0:
      print 'No devices found'
    return

  file_commands = opts.flash or opts.compare or opts.backup
  part_commands = file_commands or opts.md5 or opts.erase
  if file_commands and len(args) != 1:
    parser.error('Required filename missing')
  elif not file_commands and len(args) != 0:
    parser.error('Unexpected argument')

  if opts.backup and opts.flash:
    parser.error('Cannot backup and flash using the same file')

  if part_commands and not opts.part:
    parser.error('Partition name (-p) is not specified')

  if (not opts.backup) and file_commands and (args[0] == '-'):
    parser.error('Can only use stdin filename with --backup')

  if opts.select is None:
    _, device_list = rktalk.list_devices()
    if len(device_list) == 0:
      parser.error('No devices found')
    elif len(device_list) > 1:
      parser.error('%d devices found, but -s is not specified' %
                   len(device_list))
    bus_id, dev_id = device_list[0][:2]
  else:
    bus_id, dev_id = [int(x) for x in opts.select.split(':', 1)]

  logger = StderrLogger(verbose=0)
  if opts.verbose:
    logger.verbose = 2
  op = rktalk.RkOperation(logger, bus_id, dev_id)
  partitions = op.load_partitions()

  # Add pseudo-partitions
  max_addr = max(int(size, 16) + int(offset, 16)
                 for size, offset, name in partitions)
  partitions.append(("%08X" % max_addr, "0x%08X" % 0, "__all__"))

  min_addr = min(int(size, 16) for size, offset, name in partitions)
  if min_addr != 0:
    partitions.insert(0, ("%08X" % min_addr, "0x%08X" % 0, "__head__"))

  if not (part_commands or opts.reboot):
    print 'Partitions:'
    last_end = 0
    # Alignment is 4MB, apparently?
    ALIGNMENT_BLOCKS = 4 * 1024 * 1024 / 512

    for size, offset, name in partitions:
      atags  =[]
      if (int(offset, 16) % ALIGNMENT_BLOCKS):
        atags.append(' unaligned')
      gap = int(offset, 16) - last_end
      last_end = int(offset, 16) + int(size, 16)
      if gap and name != '__all__':
        atags.append(' gap=%.1fkB' % (gap * 512 / 1024.0))
      print ' %-20s (%s @ %s, %6.1f MB)%s' % (
        name, size, offset, int(size, 16)*512/1024.0/1024.0,
        ''.join(atags))
    return

  part_todo = list()
  if part_commands:
    for p_name in opts.part:
      for size, offset, name in partitions:
        if name == p_name or (p_name == '*' and name != '__all__'):
          p_size = int(size, 16)
          p_offset = int(offset, 16)
          part_todo.append((p_size, p_offset, name))
          if p_name != '*':
            break
      else:
        if p_name != '*':
          parser.error('Invalid partition name %r' % p_name)

  # We need at least some some progress now.
  logger.verbose = max(logger.verbose, 1)
  results = list()

  for p_size, p_offset, p_name in part_todo:
    # Generate output name
    tfile = None
    if file_commands or opts.md5:
      if (not file_commands) or (args[0] == '-'):
        # Stdin given. Use a temporary file.
        tfile = tempfile.NamedTemporaryFile(mode='rb', prefix='rktalk-',
                                            suffix='.'+p_name)
        filename = tfile.name
      elif (len(part_todo) == 1):
        # Single partition. Use file name as-is.
        filename = args[0]
      elif args[0] == '' or args[0].endswith('/'):
        # Empty or name ends in / -- use partition as filename.
        filename = args[0] + p_name
      else:
        # Else, append .part
        filename = args[0].rstrip('.') + '.' + p_name

    if opts.flash:
      # Verify flash filename right away, before erasing anything
      open(filename, 'rb').close()

    if opts.md5 or opts.backup:
      op.backup_partition(p_offset, p_size, filename,
                          verify=False)
      if opts.md5:
        summer = hashlib.md5()
        summer.update(open(filename, 'rb').read())
        results.append('%s  %s' % (summer.hexdigest(), p_name))
        if opts.backup and (tfile is not None):
          sys.stdout.write(open(filename, 'rb').read())

    if opts.erase:
      op.erase_partition(p_offset, p_size)
      pass

    if opts.flash:
      op.flash_image_file(p_offset, p_size, filename, verify=False)
      pass

    if opts.compare:
      errors = op.cmp_part_with_file(p_offset, p_size, filename)
      if errors:
        logger.print_error(
          'Verification failed: there were %d errors\n' % errors)
        return 1

  if opts.reboot:
    op.reboot()

  for r in results:
    print r

  return 0

if __name__ == '__main__':
  sys.exit(cmdline_main(sys.argv[1:]))
