#!/usr/bin/env python
"""
Script controlling processing of "image_cache" table image content
via HBase (happybase module)

Notes:
- HBase scan doesn't return keys in order, so check pointing based on "highest"
  key completed doesn't effectively work. There is completed key skipping,
  which prevents duplicate computation, but we still have to scan  the whole
  HBase table, which takes a while.

"""

import happybase
import jinja2
import logging
import mimetypes
import multiprocessing
import multiprocessing.pool
import os
import subprocess
import tempfile
from tika import detector as tika_detector
import time

from smqtk.representation.descriptor_element.local_elements import DescriptorFileElement
import smqtk.utils.bin_utils
import smqtk.utils.factors
import smqtk.utils.file_utils


HBASE_ADDRESS = '127.0.0.1'  # or an actual HBase server address
HBASE_TIMEOUT = 3600000  # one hour
HBASE_TABLE = 'image_cache'
HBASE_BINARY_COL = 'image:binary'
HBASE_BATCH_SIZE = 1000
HBASE_START_KEY = '0' * 40  # SHA1 simulation
HBASE_STOP_KEY = 'F' * 40  # SHA1 simulation

CNN_BATCH_SIZE = 10000  # Total batch of images to run in an execution of the descriptor executable at a time.
CNN_GPU_BATCH_SIZE = 100  # Number of images computed on GPU at a time
CNN_EXE = "cnn_feature_extractor"
CNN_CAFFE_MODE = '/data/kitware/caffe/source/models/bvlc_reference_caffenet/bvlc_reference_caffenet.caffemodel'
CNN_IMAGE_MEAN = '/data/kitware/smqtk/caffe_models/image_net/imagenet_mean.binaryproto'
CNN_PROTOTXT_TEMPLATE_FILE = '/data/kitware/smqtk/image_cache_cnn_compute/cnn_config.prototxt.tmpl'
CNN_PROTOTXT_TEMPLATE = jinja2.Template(open(CNN_PROTOTXT_TEMPLATE_FILE).read())

# Settings for SMQTK DescriptorFileElement construction
SMQTK_DESCRIPTOR_TYPE = 'CaffeDefaultImageNet'
SMQTK_DESCRIPTOR_SAVE_DIR = "/data/kitware/smqtk/image_cache_cnn_compute/descriptors"
SMQTK_DESCRIPTOR_DIR_SPLIT = 10

# Checkpoint information
FEED_QUEUE_MAX_SIZE = CNN_BATCH_SIZE * 2

TEMP_DIR = "/dev/shm"


mt = mimetypes.MimeTypes()


VALID_TYPES = {
    'image/tiff',
    'image/png',
    'image/jpeg',
}


def write_to_temp(img_binary):
    """
    Detect binary data content type and write to tmp file using appropriate
    suffix.

    :param img_binary: Image binary data as a string chunck.
    :type img_binary: str

    :return: Filepath to the written temp file, or None if a temp file could not be written.
    :rtype: str | None

    """
    log = logging.getLogger("compute.write_to_temp")
    ct = tika_detector.from_buffer(img_binary)
    if not ct:
        log.warn("Detected no content type (None)")
        return None
    if ct not in VALID_TYPES:
        log.warn("Invalid image type '%s'", ct)
        return None
    ext = mt.guess_extension(ct)
    if not ext:
        log.warn("Count not guess extension for type: %s", ext)
        return None
    fd, filepath = tempfile.mkstemp(suffix=ext, dir=TEMP_DIR)
    os.close(fd)
    with open(filepath, 'wb') as ofile:
        ofile.write(img_binary)
    return filepath


def async_write_temp((key, img_binary, out_q)):
    """
    Detect binary data content type and write to tmp file using appropriate
    suffix. Outputs (key, filepath) to given output Queue.

    :param key: key of the element
    :type key: str

    :param img_binary: Image binary data as a string chunck.
    :type img_binary: str

    :param out_q: Output queue.
    :type out_q: multiprocessing.Queue

    """
    log = logging.getLogger("compute.async_write_temp[key::%s]" % key)
    ct = tika_detector.from_buffer(img_binary)
    if not ct:
        log.warn("Detected no content type (None)")
        return
    if ct not in VALID_TYPES:
        log.warn("Invalid image type '%s'", ct)
        return
    ext = mt.guess_extension(ct)
    if not ext:
        log.warn("Count not guess extension for type: %s", ext)
        return
    fd, filepath = tempfile.mkstemp(suffix=ext, dir=TEMP_DIR)
    os.close(fd)
    with open(filepath, 'wb') as ofile:
        ofile.write(img_binary)

    out_q.put((key, filepath))


def make_descriptor(key):
    """
    Make a standard DescriptorFileElement based on the given key and current
    configuration.
    """
    return DescriptorFileElement(SMQTK_DESCRIPTOR_TYPE, key,
                                 SMQTK_DESCRIPTOR_SAVE_DIR,
                                 subdir_split=SMQTK_DESCRIPTOR_DIR_SPLIT)


class HBaseFeeder (multiprocessing.Process):
    """
    Uses above ``HBASE_*`` configuration properties aside from key variables
    which are provided to the constructor.

    Scans the configured HBase table from start to stop keys, feeding to the
    given queue:

        (key, filepath)

    pairs after writing the queries binary data to temp files.

    Writes a None value to the queue when it is done scanning and will not
    produce any more pairs.

    TODO:
        - Split out the temp file writing into a separate process in between
          this and the descriptor generator process.

    """

    # Number of cores to use for parallel operations, or all cores if
    # set to None.
    PARALLEL = None

    @property
    def log(self):
        return logging.getLogger("compute.HBaseFeeder")

    def __init__(self, start_key, stop_key, feed_queue, batch_size):
        super(HBaseFeeder, self).__init__(name="HBaseFeeder")
        self.start_key = start_key
        self.stop_key = stop_key
        self.queue = feed_queue
        self.batch_size = batch_size

    def run(self):
        table = happybase.Connection(HBASE_ADDRESS, timeout=HBASE_TIMEOUT)\
            .table(HBASE_TABLE)
        scan_iter = table.scan(
            row_start=self.start_key,
            row_stop=self.stop_key,
            batch_size=self.batch_size,
            columns=[HBASE_BINARY_COL]
        )

        doc_batch = {}
        #last_key = None
        for key, doc in scan_iter:
            # Normalize hex casing
            key = key.lower()

            # This passes consistently. Commenting for optimization.
            #assert key > last_key, \
            #    "Found an iteration order exception: '%s' >! '%s'" \
            #    % (key, last_key)

            # Make a temporary DescriptorFileElement to see if this key has been computed before or not.
            if make_descriptor(key).has_vector():
                self.log.debug("vector with key '%s' already computed", key)
                continue

            binary = doc[HBASE_BINARY_COL]

            if not binary:
               self.log.debug("Skipping '%s', zero binary data", key)
               continue

            doc_batch[key] = binary

            if len(doc_batch) >= self.batch_size:
                self.log.info("Recieved batch of %d elements from HBase, writing to files",
                              self.batch_size)

                # keys, binaries = zip(*doc_batch.iteritems())
                # pool = multiprocessing.Pool(self.PARALLEL)
                # temp_files = pool.map(write_to_temp, binaries)
                # self.log.info("Pushing values to queue")
                # for k, f in zip(keys, temp_files):
                #     if f is not None:
                #         self.queue.put((k, f))
                # pool.close()
                # pool.join()

                pool = multiprocessing.pool.ThreadPool(self.PARALLEL)
                pool.map(async_write_temp, zip(*zip(*doc_batch.iteritems()) + [[self.queue]*len(doc_batch)]))
                pool.close()
                pool.join()

                self.log.info("Cleaning up processes")
                del pool,  # temp_files, key, binaries
                doc_batch = {}

        # Write anything remaining in the batch structure
        #keys, binaries = zip(*doc_batch.iteritems())
        #pool = multiprocessing.Pool(self.PARALLEL)
        #temp_files = pool.map(write_to_temp, binaries)
        #self.log.info("Pushing values to queue")
        #for k, f in zip(keys, temp_files):
        #    if f is not None:
        #        self.queue.put((k, f))
        #pool.close()
        #pool.join()
        pool = multiprocessing.pool.ThreadPool(self.PARALLEL)
        pool.map(async_write_temp, zip(*zip(*doc_batch.iteritems()) + [[self.queue]*len(doc_batch)]))
        pool.close()
        pool.join()

        self.queue.put(None)


def set_descriptor(p):
    """
    Create SMQTK DescriptorFileElement instance for a given descriptor vector.
    Intended for use within a pool.map call, thus the tuple expansion of input.
    """
    key, vector = p
    e = make_descriptor(key)
    e.set_vector(vector)
    return e


class CaffeDescriptorGenerator (multiprocessing.Process):
    """
    Compute CNN descriptors on files fed to this process via the provided
    input queue. We expect (key, img_file) pairs, where key is the SHA1 hash
    of the file. We output the greatest SHA1 hash of a completed batch of pairs
    to the provided ``complete_queue`` queue. This hash can be used as a
    checkpoint for the feeder so we don't reprocess material that we have
    already finished.
    """

    # Number of cores to use for parallel operations, or all if set to None
    PARALLEL = None

    @property
    def log(self):
        return logging.getLogger('compute.DescriptorGenerator')

    def __init__(self, input_queue, complete_queue, batch_size, gpu_batch_size):
        super(CaffeDescriptorGenerator, self).__init__(name="CaffeDescriptorGenerator")
        self.in_queue = input_queue
        self.complete_queue = complete_queue
        self.batch_size = batch_size
        self.gpu_batch_size = gpu_batch_size

    def run(self):
        running = True
        batch = {}
        while running:
            try:
                input = self.in_queue.get()

                if input is None:
                    self.log.info("Received terminal message. Closing down.")
                    running = False
                    continue

                key, temp_file = input

                # Make a temporary DescriptorFileElement to see if this key has been computed before or not.
                if make_descriptor(key).has_vector():
                    self.log.debug("vector with key '%s' already computed", key)
                    os.remove(temp_file)
                    continue

                batch[key] = temp_file

                if len(batch) >= self.batch_size:
                    self.process_batch(batch, self.gpu_batch_size)
                    self.complete_queue.put(max(batch))
                    batch.clear()

            except IOError, ex:
                self.log.warning("Failed to pull from input queue, closing down.")
                running = False

        if batch:
            # Finish up what ever is currently in the batch
            # - Pick largest factor of remaining batch size less than the
            #   configured GPU batch size.
            gpu_b_size = \
                max([f for f in smqtk.utils.factors.factors(len(batch))
                     if f <= self.gpu_batch_size])
            self.log.info("Computing remaining batch of size %d (GPU batch: %d)",
                          len(batch), gpu_b_size)
            self.process_batch(batch, gpu_b_size)


    def process_batch(self, batch, gpu_batch_size):
        assert len(batch) % gpu_batch_size == 0, \
            "GPU Batch size does not evenly divide the given computation batch"
        cnn_minibatch_size = len(batch) / gpu_batch_size

        keys, temp_files = zip(*batch.items())

        # Write out path-list file
        # - Caffe needs the trailing '0', else there will be a non-descript
        #   segfault.
        self.log.info("Generating work file path list")
        fd, list_filepath = tempfile.mkstemp(suffix='.txt', dir=TEMP_DIR)
        os.close(fd)
        with open(list_filepath, 'w') as ofile:
            for tf in temp_files:
                ofile.write('%s 0\n' % tf)

        # generate prototxt configuration file
        self.log.info("Generating prototext config file")
        config_str = CNN_PROTOTXT_TEMPLATE.render(**{
            "image_mean_filepath": CNN_IMAGE_MEAN,
            "image_filelist_filepath": list_filepath,
            "batch_size": gpu_batch_size,
        })
        fd, protoconfig_filepath = tempfile.mkstemp(suffix='.prototxt', dir=TEMP_DIR)
        os.close(fd)
        with open(protoconfig_filepath, 'w')  as ofile:
            ofile.write(config_str)

        # Call executable
        fd, output_filebase = tempfile.mkstemp(dir=TEMP_DIR)
        os.close(fd)
        os.remove(output_filebase)
        # The output file that actually gets generated
        output_csv = output_filebase + '.csv'
        call_args = [
            CNN_EXE, CNN_CAFFE_MODE, protoconfig_filepath, 'fc7',
            output_filebase, str(cnn_minibatch_size), 'csv', 'GPU'
        ]
        self.log.info("Call args: %s", call_args)
        proc_cnn = subprocess.Popen(call_args)
        rc = proc_cnn.wait()
        if rc:
            raise RuntimeError("Failed to execute CNN executable with return code: %s" % rc)

        # Parse output file into SMQTK DescriptorElement instances
        self.log.info("Parsing output descriptors")
        pool = multiprocessing.Pool(self.PARALLEL)
        d_elems = pool.map(set_descriptor,
                           zip(keys, smqtk.utils.file_utils.iter_csv_file(output_csv)))

        # Remove temp files used
        self.log.info("Cleaning up")
        pool.map(os.remove, temp_files)
        pool.close()
        pool.join()
        os.remove(list_filepath)
        os.remove(protoconfig_filepath)
        os.remove(output_csv)

        self.log.info("Returning elements")
        return d_elems


def run():
    log = logging.getLogger("compute.run2")

    log.info("Feed queue max size: %d", FEED_QUEUE_MAX_SIZE)
    f_queue = multiprocessing.Queue(FEED_QUEUE_MAX_SIZE)
    c_queue = multiprocessing.Queue()  # This queue will never effectively be that large

    feeder = HBaseFeeder(HBASE_START_KEY, HBASE_STOP_KEY, f_queue, HBASE_BATCH_SIZE)
    generator = CaffeDescriptorGenerator(f_queue, c_queue, CNN_BATCH_SIZE, CNN_GPU_BATCH_SIZE)

    feeder.start()
    generator.start()

    log.info("Waiting for worker processes to complete.")
    feeder.join()
    generator.join()


if __name__ == "__main__":
    smqtk.utils.bin_utils.initialize_logging(logging.getLogger('compute'), logging.INFO)
    run()
