import importlib.util
import pathlib
import unittest
from types import SimpleNamespace
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('storage_probe',pathlib.Path(__file__).parents[2]/'fleet-storage-probe.py')
probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.table='1 0 8:1 / / rw - ext4 /dev/sda1 rw\n2 1 8:2 / /extra rw - ext4 /dev/sdb rw\n'
        self.stat=patch.object(pathlib.Path,'stat',return_value=SimpleNamespace(st_dev=2));self.stat.start()
        self.resolve=patch.object(pathlib.Path,'resolve',lambda p,strict=False:p);self.resolve.start()
        self.read=patch.object(pathlib.Path,'read_text',lambda p:self.table);self.read.start()
        self.vfs=patch.object(probe.os,'statvfs',return_value=SimpleNamespace(f_bavail=7,f_frsize=4096));self.vfs.start()
    def tearDown(self):
        self.vfs.stop();self.read.stop();self.resolve.stop();self.stat.stop()
    def test_destination_volume_not_root_and_available_not_total(self):
        info=probe.observe('http://remote','fleet','/extra/qdrant','/extra','/dev/sdb')
        self.assertEqual(info['available_bytes'],28672);self.assertEqual(info['device'],'/dev/sdb')
    def test_wrong_device_or_wrong_mount_or_path_refused(self):
        for path,mount,device in [('/extra/qdrant','/extra','/dev/sda1'),
                                  ('/extra/qdrant','/','/dev/sda1'),('/other','/extra','/dev/sdb')]:
            with self.assertRaises(ValueError):probe.observe('x','fleet',path,mount,device)
    def test_deeper_mount_not_silently_counted_as_parent(self):
        self.table+='3 2 8:3 / /extra/qdrant rw - ext4 /dev/sdc rw\n'
        with self.assertRaises(ValueError):probe.observe('x','fleet','/extra/qdrant','/extra','/dev/sdb')
    def test_escaped_mount_names(self):
        self.table='2 1 8:2 / /extra\\040disk rw - ext4 /dev/sdb rw\n'
        info=probe.observe('x','fleet','/extra disk/qdrant','/extra disk','/dev/sdb')
        self.assertEqual(info['mount'],'/extra disk')
    def test_stat_device_mismatch_refused(self):
        with patch.object(pathlib.Path,'stat',side_effect=[SimpleNamespace(st_dev=1),SimpleNamespace(st_dev=2)]):
            with self.assertRaises(ValueError):probe.observe('x','fleet','/extra/qdrant','/extra','/dev/sdb')
