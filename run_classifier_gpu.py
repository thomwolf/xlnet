from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

from os.path import join
from absl import flags
import os
import sys
import csv
import collections
import numpy as np
import time
import math
import json
import random
from copy import copy
from collections import defaultdict as dd

import absl.logging as _logging  # pylint: disable=unused-import
import tensorflow as tf

import sentencepiece as spm

from data_utils import SEP_ID, VOCAB_SIZE, CLS_ID
import model_utils
import function_builder
from classifier_utils import PaddingInputExample
from classifier_utils import convert_single_example
from prepro_utils import preprocess_text, encode_ids
from gpu_utils import assign_to_gpu, average_grads_and_vars

# GPU config
flags.DEFINE_integer("num_hosts", default=1,
      help="Number of hosts")
flags.DEFINE_integer("num_core_per_host", default=8,
      help="Number of cores per host")
flags.DEFINE_bool("use_tpu", default=False,
      help="Whether to use TPUs for training.")

# Model
flags.DEFINE_string("model_config_path", default=None,
      help="Model config path.")
flags.DEFINE_float("dropout", default=0.1,
      help="Dropout rate.")
flags.DEFINE_float("dropatt", default=0.1,
      help="Attention dropout rate.")
flags.DEFINE_integer("clamp_len", default=-1,
      help="Clamp length")
flags.DEFINE_string("summary_type", default="last",
      help="Method used to summarize a sequence into a compact vector.")
flags.DEFINE_bool("use_summ_proj", default=True,
      help="Whether to use projection for summarizing sequences.")
flags.DEFINE_bool("use_bfloat16", False,
      help="Whether to use bfloat16.")

# Parameter initialization
flags.DEFINE_enum("init", default="normal",
      enum_values=["normal", "uniform"],
      help="Initialization method.")
flags.DEFINE_float("init_std", default=0.02,
      help="Initialization std when init is normal.")
flags.DEFINE_float("init_range", default=0.1,
      help="Initialization std when init is uniform.")

# I/O paths
flags.DEFINE_bool("overwrite_data", default=False,
      help="If False, will use cached data if available.")
flags.DEFINE_string("init_checkpoint", default=None,
      help="checkpoint path for initializing the model. "
      "Could be a pretrained model or a finetuned model.")
flags.DEFINE_string("output_dir", default="",
      help="Output dir for TF records.")
flags.DEFINE_string("spiece_model_file", default="",
      help="Sentence Piece model path.")
flags.DEFINE_string("model_dir", default="",
      help="Directory for saving the finetuned model.")
flags.DEFINE_string("data_dir", default="",
      help="Directory for input data.")

# # TPUs and machines
# flags.DEFINE_bool("use_tpu", default=False, help="whether to use TPU.")
# flags.DEFINE_integer("num_hosts", default=1, help="How many TPU hosts.")
# flags.DEFINE_integer("num_core_per_host", default=8,
#       help="8 for TPU v2 and v3-8, 16 for larger TPU v3 pod. In the context "
#       "of GPU training, it refers to the number of GPUs used.")
# flags.DEFINE_string("tpu_job_name", default=None, help="TPU worker job name.")
# flags.DEFINE_string("tpu", default=None, help="TPU name.")
# flags.DEFINE_string("tpu_zone", default=None, help="TPU zone.")
# flags.DEFINE_string("gcp_project", default=None, help="gcp project.")
# flags.DEFINE_string("master", default=None, help="master")


# training
flags.DEFINE_bool("do_train", default=False, help="whether to do training")
flags.DEFINE_integer("train_steps", default=1000,
      help="Number of training steps")
flags.DEFINE_integer("warmup_steps", default=0, help="number of warmup steps")
flags.DEFINE_integer("iterations", default=100,
      help="Number of iterations per repeat loop.")
flags.DEFINE_float("learning_rate", default=1e-5, help="initial learning rate")
flags.DEFINE_float("lr_layer_decay_rate", 1.0,
                   "Top layer: lr[L] = FLAGS.learning_rate."
                   "Low layer: lr[l-1] = lr[l] * lr_layer_decay_rate.")
flags.DEFINE_float("min_lr_ratio", default=0.0,
      help="min lr ratio for cos decay.")
flags.DEFINE_float("clip", default=1.0, help="Gradient clipping")
flags.DEFINE_integer("max_save", default=0,
      help="Max number of checkpoints to save. Use 0 to save all.")
flags.DEFINE_integer("log_step_count_steps", default=100,
      help="Log every X steps.")
flags.DEFINE_integer("save_steps", default=100,
      help="Save the model for every save_steps. "
      "If None, not to save any model.")
flags.DEFINE_integer("train_batch_size", default=8,
      help="Batch size for training")
flags.DEFINE_float("weight_decay", default=0.00, help="Weight decay rate")
flags.DEFINE_float("adam_epsilon", default=1e-8, help="Adam epsilon")
flags.DEFINE_string("decay_method", default="poly", help="poly or cos")

# evaluation
flags.DEFINE_bool("do_eval", default=False, help="whether to do eval")
flags.DEFINE_bool("do_predict", default=False, help="whether to do prediction")
flags.DEFINE_float("predict_threshold", default=0,
      help="Threshold for binary prediction.")
flags.DEFINE_string("eval_split", default="dev", help="could be dev or test")
flags.DEFINE_integer("eval_batch_size", default=128,
      help="batch size for evaluation")
flags.DEFINE_integer("predict_batch_size", default=128,
      help="batch size for prediction.")
flags.DEFINE_string("predict_dir", default=None,
      help="Dir for saving prediction files.")
flags.DEFINE_bool("eval_all_ckpt", default=False,
      help="Eval all ckpts. If False, only evaluate the last one.")
flags.DEFINE_string("predict_ckpt", default=None,
      help="Ckpt path for do_predict. If None, use the last one.")

# task specific
flags.DEFINE_string("task_name", default=None, help="Task name")
flags.DEFINE_integer("max_seq_length", default=128, help="Max sequence length")
flags.DEFINE_integer("shuffle_buffer", default=2048,
      help="Buffer size used for shuffle.")
flags.DEFINE_integer("num_passes", default=1,
      help="Num passes for processing training data. "
      "This is use to batch data without loss for TPUs.")
flags.DEFINE_bool("uncased", default=False,
      help="Use uncased.")
flags.DEFINE_string("cls_scope", default=None,
      help="Classifier layer scope.")
flags.DEFINE_bool("is_regression", default=False,
      help="Whether it's a regression task.")

flags.DEFINE_integer("seed", default=42,
      help="Seed.")

flags.DEFINE_string('server_ip', default='', help="Can be used for distant debugging.")
flags.DEFINE_string('server_port', default='', help="Can be used for distant debugging.")

FLAGS = flags.FLAGS



class InputExample(object):
  """A single training/test example for simple sequence classification."""

  def __init__(self, guid, text_a, text_b=None, label=None):
    """Constructs a InputExample.
    Args:
      guid: Unique id for the example.
      text_a: string. The untokenized text of the first sequence. For single
        sequence tasks, only this sequence must be specified.
      text_b: (Optional) string. The untokenized text of the second sequence.
        Only must be specified for sequence pair tasks.
      label: (Optional) string. The label of the example. This should be
        specified for train and dev examples, but not for test examples.
    """
    self.guid = guid
    self.text_a = text_a
    self.text_b = text_b
    self.label = label


class DataProcessor(object):
  """Base class for data converters for sequence classification data sets."""

  def get_train_examples(self, data_dir):
    """Gets a collection of `InputExample`s for the train set."""
    raise NotImplementedError()

  def get_dev_examples(self, data_dir):
    """Gets a collection of `InputExample`s for the dev set."""
    raise NotImplementedError()

  def get_test_examples(self, data_dir):
    """Gets a collection of `InputExample`s for prediction."""
    raise NotImplementedError()

  def get_labels(self):
    """Gets the list of labels for this data set."""
    raise NotImplementedError()

  @classmethod
  def _read_tsv(cls, input_file, quotechar=None):
    """Reads a tab separated value file."""
    with tf.gfile.Open(input_file, "r") as f:
      reader = csv.reader(f, delimiter="\t", quotechar=quotechar)
      lines = []
      for line in reader:
        if len(line) == 0: continue
        lines.append(line)
      return lines


class GLUEProcessor(DataProcessor):
  def __init__(self):
    self.train_file = "train.tsv"
    self.dev_file = "dev.tsv"
    self.test_file = "test.tsv"
    self.label_column = None
    self.text_a_column = None
    self.text_b_column = None
    self.contains_header = True
    self.test_text_a_column = None
    self.test_text_b_column = None
    self.test_contains_header = True

  def get_train_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, self.train_file)), "train")

  def get_dev_examples(self, data_dir):
    """See base class."""
    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, self.dev_file)), "dev")

  def get_test_examples(self, data_dir):
    """See base class."""
    if self.test_text_a_column is None:
      self.test_text_a_column = self.text_a_column
    if self.test_text_b_column is None:
      self.test_text_b_column = self.text_b_column

    return self._create_examples(
        self._read_tsv(os.path.join(data_dir, self.test_file)), "test")

  def get_labels(self):
    """See base class."""
    return ["0", "1"]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0 and self.contains_header and set_type != "test":
        continue
      if i == 0 and self.test_contains_header and set_type == "test":
        continue
      guid = "%s-%s" % (set_type, i)

      a_column = (self.text_a_column if set_type != "test" else
          self.test_text_a_column)
      b_column = (self.text_b_column if set_type != "test" else
          self.test_text_b_column)

      # there are some incomplete lines in QNLI
      if len(line) <= a_column:
        tf.logging.warning('Incomplete line, ignored.')
        continue
      text_a = line[a_column]

      if b_column is not None:
        if len(line) <= b_column:
          tf.logging.warning('Incomplete line, ignored.')
          continue
        text_b = line[b_column]
      else:
        text_b = None

      if set_type == "test":
        label = self.get_labels()[0]
      else:
        if len(line) <= self.label_column:
          tf.logging.warning('Incomplete line, ignored.')
          continue
        label = line[self.label_column]
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))
    return examples


class Yelp5Processor(DataProcessor):
  def get_train_examples(self, data_dir):
    return self._create_examples(os.path.join(data_dir, "train.csv"))

  def get_dev_examples(self, data_dir):
    return self._create_examples(os.path.join(data_dir, "test.csv"))

  def get_labels(self):
    """See base class."""
    return ["1", "2", "3", "4", "5"]

  def _create_examples(self, input_file):
    """Creates examples for the training and dev sets."""
    examples = []
    with tf.gfile.Open(input_file) as f:
      reader = csv.reader(f)
      for i, line in enumerate(reader):

        label = line[0]
        text_a = line[1].replace('""', '"').replace('\\"', '"')
        examples.append(
            InputExample(guid=str(i), text_a=text_a, text_b=None, label=label))
    return examples


class ImdbProcessor(DataProcessor):
  def get_labels(self):
    return ["neg", "pos"]

  def get_train_examples(self, data_dir):
    return self._create_examples(os.path.join(data_dir, "train"))

  def get_dev_examples(self, data_dir):
    return self._create_examples(os.path.join(data_dir, "test"))

  def _create_examples(self, data_dir):
    examples = []
    for label in ["neg", "pos"]:
      cur_dir = os.path.join(data_dir, label)
      for filename in tf.gfile.ListDirectory(cur_dir):
        if not filename.endswith("txt"): continue

        path = os.path.join(cur_dir, filename)
        with tf.gfile.Open(path) as f:
          text = f.read().strip().replace("<br />", " ")
        examples.append(InputExample(
            guid="unused_id", text_a=text, text_b=None, label=label))
    return examples


class MnliMatchedProcessor(GLUEProcessor):
  def __init__(self):
    super(MnliMatchedProcessor, self).__init__()
    self.dev_file = "dev_matched.tsv"
    self.test_file = "test_matched.tsv"
    self.label_column = -1
    self.text_a_column = 8
    self.text_b_column = 9

  def get_labels(self):
    return ["contradiction", "entailment", "neutral"]


class MnliMismatchedProcessor(MnliMatchedProcessor):
  def __init__(self):
    super(MnliMismatchedProcessor, self).__init__()
    self.dev_file = "dev_mismatched.tsv"
    self.test_file = "test_mismatched.tsv"


class StsbProcessor(GLUEProcessor):
  def __init__(self):
    super(StsbProcessor, self).__init__()
    self.label_column = 9
    self.text_a_column = 7
    self.text_b_column = 8

  def get_labels(self):
    return [0.0]

  def _create_examples(self, lines, set_type):
    """Creates examples for the training and dev sets."""
    examples = []
    for (i, line) in enumerate(lines):
      if i == 0 and self.contains_header and set_type != "test":
        continue
      if i == 0 and self.test_contains_header and set_type == "test":
        continue
      guid = "%s-%s" % (set_type, i)

      a_column = (self.text_a_column if set_type != "test" else
          self.test_text_a_column)
      b_column = (self.text_b_column if set_type != "test" else
          self.test_text_b_column)

      # there are some incomplete lines in QNLI
      if len(line) <= a_column:
        tf.logging.warning('Incomplete line, ignored.')
        continue
      text_a = line[a_column]

      if b_column is not None:
        if len(line) <= b_column:
          tf.logging.warning('Incomplete line, ignored.')
          continue
        text_b = line[b_column]
      else:
        text_b = None

      if set_type == "test":
        label = self.get_labels()[0]
      else:
        if len(line) <= self.label_column:
          tf.logging.warning('Incomplete line, ignored.')
          continue
        label = float(line[self.label_column])
      examples.append(
          InputExample(guid=guid, text_a=text_a, text_b=text_b, label=label))

    return examples


def file_based_convert_examples_to_features(
    examples, label_list, max_seq_length, tokenize_fn, output_file,
    num_passes=1):
  """Convert a set of `InputExample`s to a TFRecord file."""

  # do not create duplicated records
  if tf.gfile.Exists(output_file) and not FLAGS.overwrite_data:
    tf.logging.info("Do not overwrite tfrecord {} exists.".format(output_file))
    return

  tf.logging.info("Create new tfrecord {}.".format(output_file))

  writer = tf.python_io.TFRecordWriter(output_file)

#   np.random.shuffle(examples)
  if num_passes > 1:
    examples *= num_passes

  for (ex_index, example) in enumerate(examples):
    if ex_index % 10000 == 0:
      tf.logging.info("Writing example {} of {}".format(ex_index,
                                                        len(examples)))

    feature = convert_single_example(ex_index, example, label_list,
                                     max_seq_length, tokenize_fn)

    def create_int_feature(values):
      f = tf.train.Feature(int64_list=tf.train.Int64List(value=list(values)))
      return f

    def create_float_feature(values):
      f = tf.train.Feature(float_list=tf.train.FloatList(value=list(values)))
      return f

    features = collections.OrderedDict()
    features["input_ids"] = create_int_feature(feature.input_ids)
    features["input_mask"] = create_float_feature(feature.input_mask)
    features["segment_ids"] = create_int_feature(feature.segment_ids)
    if label_list is not None:
      features["label_ids"] = create_int_feature([feature.label_id])
    else:
      features["label_ids"] = create_float_feature([float(feature.label_id)])
    features["is_real_example"] = create_int_feature(
        [int(feature.is_real_example)])

    tf_example = tf.train.Example(features=tf.train.Features(feature=features))
    writer.write(tf_example.SerializeToString())
  writer.close()


def file_based_input_fn_builder(input_file, seq_length, is_training,
                                drop_remainder):
  """Creates an `input_fn` closure to be passed to TPUEstimator."""


  name_to_features = {
      "input_ids": tf.FixedLenFeature([seq_length], tf.int64),
      "input_mask": tf.FixedLenFeature([seq_length], tf.float32),
      "segment_ids": tf.FixedLenFeature([seq_length], tf.int64),
      "label_ids": tf.FixedLenFeature([], tf.int64),
      "is_real_example": tf.FixedLenFeature([], tf.int64),
  }
  if FLAGS.is_regression:
    name_to_features["label_ids"] = tf.FixedLenFeature([], tf.float32)

  tf.logging.info("Input tfrecord file {}".format(input_file))

  def _decode_record(record, name_to_features):
    """Decodes a record to a TensorFlow example."""
    example = tf.parse_single_example(record, name_to_features)

    # tf.Example only supports tf.int64, but the TPU only supports tf.int32.
    # So cast all int64 to int32.
    for name in list(example.keys()):
      t = example[name]
      if t.dtype == tf.int64:
        t = tf.cast(t, tf.int32)
      example[name] = t

    return example

  def input_fn(params, input_context=None):
    """The actual input function."""
    if FLAGS.use_tpu:
      batch_size = params["batch_size"]
    elif is_training:
      batch_size = FLAGS.train_batch_size
    elif FLAGS.do_eval:
      batch_size = FLAGS.eval_batch_size
    else:
      batch_size = FLAGS.predict_batch_size

    d = tf.data.TFRecordDataset(input_file)
    # Shard the dataset to difference devices
    if input_context is not None:
      tf.logging.info("Input pipeline id %d out of %d",
          input_context.input_pipeline_id, input_context.num_replicas_in_sync)
      d = d.shard(input_context.num_input_pipelines,
                  input_context.input_pipeline_id)

    # For training, we want a lot of parallel reading and shuffling.
    # For eval, we want no shuffling and parallel reading doesn't matter.
    if is_training:
    #   d = d.shuffle(buffer_size=FLAGS.shuffle_buffer)
      d = d.repeat()

    d = d.apply(
        tf.contrib.data.map_and_batch(
            lambda record: _decode_record(record, name_to_features),
            batch_size=batch_size,
            drop_remainder=drop_remainder))

    return d

  return input_fn


def get_model_fn(n_class):
  def model_fn(features, labels, is_training):
    # #### Training or Evaluation
    # is_training = (mode == tf.estimator.ModeKeys.TRAIN)

    #### Get loss from inputs
    if FLAGS.is_regression:
      (total_loss, per_example_loss, logits, hidden_states, special
          ) = function_builder.get_regression_loss(FLAGS, features, is_training)
    else:
      (total_loss, per_example_loss, logits, hidden_states, special
          ) = function_builder.get_classification_loss(
          FLAGS, features, n_class, is_training)

    tf.summary.scalar('total_loss', total_loss)

    #### Check model parameters
    num_params = sum([np.prod(v.shape) for v in tf.trainable_variables()])
    tf.logging.info('#params: {}'.format(num_params))

    all_vars = tf.trainable_variables()
    grads = tf.gradients(total_loss, all_vars)
    grads_and_vars = list(zip(grads, all_vars))

    return total_loss, grads_and_vars, features, hidden_states, special

  return model_fn

def single_core_graph(is_training, features, label_list=None):
  model_fn = get_model_fn(len(label_list) if label_list is not None else None)

  model_ret = model_fn(
      features=features,
      labels=None,
      is_training=is_training)

  return model_ret

def main(_):
  if FLAGS.server_ip and FLAGS.server_port:
      # Distant debugging - see https://code.visualstudio.com/docs/python/debugging#_attach-to-a-local-script
      import ptvsd
      print("Waiting for debugger attach")
      ptvsd.enable_attach(address=(FLAGS.server_ip, FLAGS.server_port), redirect_output=True)
      ptvsd.wait_for_attach()

  tf.set_random_seed(FLAGS.seed)
  numpy.random.seed(FLAGS.seed)

  tf.logging.set_verbosity(tf.logging.INFO)

  #### Validate flags
  if FLAGS.save_steps is not None:
    FLAGS.iterations = min(FLAGS.iterations, FLAGS.save_steps)

  if FLAGS.do_predict:
    predict_dir = FLAGS.predict_dir
    if not tf.gfile.Exists(predict_dir):
      tf.gfile.MakeDirs(predict_dir)

  processors = {
      "mnli_matched": MnliMatchedProcessor,
      "mnli_mismatched": MnliMismatchedProcessor,
      'sts-b': StsbProcessor,
      'imdb': ImdbProcessor,
      "yelp5": Yelp5Processor
  }

  if not FLAGS.do_train and not FLAGS.do_eval and not FLAGS.do_predict:
    raise ValueError(
        "At least one of `do_train`, `do_eval, `do_predict` or "
        "`do_submit` must be True.")

  if not tf.gfile.Exists(FLAGS.output_dir):
    tf.gfile.MakeDirs(FLAGS.output_dir)

  if not tf.gfile.Exists(FLAGS.model_dir):
    tf.gfile.MakeDirs(FLAGS.model_dir)

#   ########################### LOAD PT model
#   ########################### LOAD PT model
#   import torch
#   from pytorch_transformers import CONFIG_NAME, TF_WEIGHTS_NAME, XLNetTokenizer, XLNetConfig, XLNetForSequenceClassification

#   save_path = os.path.join(FLAGS.model_dir, TF_WEIGHTS_NAME)
#   tf.logging.info("Model loaded from path: {}".format(save_path))

#   device = torch.device("cuda", 4)
#   config = XLNetConfig.from_pretrained('xlnet-large-cased', finetuning_task=u'sts-b')
#   config_path = os.path.join(FLAGS.model_dir, CONFIG_NAME)
#   config.to_json_file(config_path)
#   pt_model = XLNetForSequenceClassification.from_pretrained(FLAGS.model_dir, from_tf=True, num_labels=1)
#   pt_model.to(device)
#   pt_model = torch.nn.DataParallel(pt_model, device_ids=[4, 5, 6, 7])

#   from torch.optim import Adam
#   optimizer = Adam(pt_model.parameters(), lr=0.001, betas=(0.9, 0.999),
#                     eps=FLAGS.adam_epsilon, weight_decay=FLAGS.weight_decay,
#                     amsgrad=False)
#   ########################### LOAD PT model
#   ########################### LOAD PT model

  task_name = FLAGS.task_name.lower()

  if task_name not in processors:
    raise ValueError("Task not found: %s" % (task_name))

  processor = processors[task_name]()
  label_list = processor.get_labels() if not FLAGS.is_regression else None

  sp = spm.SentencePieceProcessor()
  sp.Load(FLAGS.spiece_model_file)
  def tokenize_fn(text):
    text = preprocess_text(text, lower=FLAGS.uncased)
    return encode_ids(sp, text)

  # run_config = model_utils.configure_tpu(FLAGS)

#   model_fn = get_model_fn(len(label_list) if label_list is not None else None)

  spm_basename = os.path.basename(FLAGS.spiece_model_file)

  # If TPU is not available, this will fall back to normal Estimator on CPU
  # or GPU.
  # estimator = tf.estimator.Estimator(
  #     model_fn=model_fn,
  #     config=run_config)

  if FLAGS.do_train:
    train_file_base = "{}.len-{}.train.tf_record".format(
        spm_basename, FLAGS.max_seq_length)
    train_file = os.path.join(FLAGS.output_dir, train_file_base)
    tf.logging.info("Use tfrecord file {}".format(train_file))

    train_examples = processor.get_train_examples(FLAGS.data_dir)
    tf.logging.info("Num of train samples: {}".format(len(train_examples)))

    file_based_convert_examples_to_features(
        train_examples, label_list, FLAGS.max_seq_length, tokenize_fn,
        train_file, FLAGS.num_passes)

    train_input_fn = file_based_input_fn_builder(
        input_file=train_file,
        seq_length=FLAGS.max_seq_length,
        is_training=True,
        drop_remainder=True)

    # estimator.train(input_fn=train_input_fn, max_steps=FLAGS.train_steps)

    ##### Create input tensors / placeholders
    bsz_per_core = FLAGS.train_batch_size // FLAGS.num_core_per_host

    params = {
        "batch_size": FLAGS.train_batch_size # the whole batch
    }
    train_set = train_input_fn(params)

    example = train_set.make_one_shot_iterator().get_next()
    if FLAGS.num_core_per_host > 1:
      examples = [{} for _ in range(FLAGS.num_core_per_host)]
      for key in example.keys():
        vals = tf.split(example[key], FLAGS.num_core_per_host, 0)
        for device_id in range(FLAGS.num_core_per_host):
          examples[device_id][key] = vals[device_id]
    else:
      examples = [example]

    ##### Create computational graph
    tower_losses, tower_grads_and_vars, tower_inputs, tower_hidden_states, tower_special = [], [], [], [], []

    for i in range(FLAGS.num_core_per_host):
      reuse = True if i > 0 else None
      with tf.device(assign_to_gpu(i, "/gpu:0")), \
          tf.variable_scope(tf.get_variable_scope(), reuse=reuse):

        loss_i, grads_and_vars_i, inputs_i, hidden_states_i, special_i = single_core_graph(
            is_training=True,
            features=examples[i],
            label_list=label_list)

        tower_losses.append(loss_i)
        tower_grads_and_vars.append(grads_and_vars_i)
        tower_inputs.append(inputs_i)
        tower_hidden_states.append(hidden_states_i)
        tower_special.append(special_i)

    ## average losses and gradients across towers
    if len(tower_losses) > 1:
      loss = tf.add_n(tower_losses) / len(tower_losses)
      grads_and_vars = average_grads_and_vars(tower_grads_and_vars)
      inputs = dict((n, tf.concat([t[n] for t in tower_inputs], 0)) for n in tower_inputs[0])
      hidden_states = list(tf.concat(t, 0) for t in zip(*tower_hidden_states))
      special = tf.concat(tower_special, 0)
    else:
      loss = tower_losses[0]
      grads_and_vars = tower_grads_and_vars[0]
      inputs = tower_inputs[0]
      hidden_states = tower_hidden_states[0]
      special = tower_special[0]

    # Summaries
    merged = tf.summary.merge_all()

    ## get train op
    train_op, learning_rate, gnorm = model_utils.get_train_op(FLAGS, None,
        grads_and_vars=grads_and_vars)
    global_step = tf.train.get_global_step()

    ##### Training loop
    saver = tf.train.Saver()

    gpu_options = tf.GPUOptions(allow_growth=True)

    #### load pretrained models
    model_utils.init_from_checkpoint(FLAGS, global_vars=True)

    writer = tf.summary.FileWriter(logdir=FLAGS.model_dir, graph=tf.get_default_graph())
    with tf.Session(config=tf.ConfigProto(allow_soft_placement=True,
        gpu_options=gpu_options)) as sess:
      sess.run(tf.global_variables_initializer())


      ########################### LOAD PT model
    #   import torch
    #   from pytorch_transformers import CONFIG_NAME, TF_WEIGHTS_NAME, WEIGHTS_NAME, XLNetTokenizer, XLNetConfig, XLNetForSequenceClassification, BertAdam

    #   save_path = os.path.join(FLAGS.model_dir, TF_WEIGHTS_NAME)
    #   saver.save(sess, save_path)
    #   tf.logging.info("Model saved in path: {}".format(save_path))

    #   device = torch.device("cuda", 4)
    #   config = XLNetConfig.from_pretrained('xlnet-large-cased', finetuning_task=u'sts-b', num_labels=1)
    #   tokenizer = XLNetTokenizer.from_pretrained('xlnet-large-cased')
    #   config_path = os.path.join(FLAGS.model_dir, CONFIG_NAME)
    #   config.to_json_file(config_path)
    #   # pt_model = XLNetForSequenceClassification.from_pretrained('xlnet-large-cased', num_labels=1)
    #   pt_model = XLNetForSequenceClassification.from_pretrained(FLAGS.model_dir, from_tf=True)
    #   pt_model.to(device)
    #   pt_model = torch.nn.DataParallel(pt_model, device_ids=[4, 5, 6, 7])
    #   from torch.optim import Adam
    #   optimizer = Adam(pt_model.parameters(), lr=0.001, betas=(0.9, 0.999),
    #                    eps=FLAGS.adam_epsilon, weight_decay=FLAGS.weight_decay,
    #                    amsgrad=False)
    #   optimizer = BertAdam(pt_model.parameters(), lr=FLAGS.learning_rate, t_total=FLAGS.train_steps, warmup=FLAGS.warmup_steps / FLAGS.train_steps,
    #                        eps=FLAGS.adam_epsilon, weight_decay=FLAGS.weight_decay)

        ##### PYTORCH
        #########

      fetches = [loss, global_step, gnorm, learning_rate, train_op, merged, inputs, hidden_states, special]

      total_loss, total_loss_pt, prev_step, gnorm_pt = 0., 0., -1, 0.0
      while True:
        feed_dict = {}
        # for i in range(FLAGS.num_core_per_host):
        #   for key in tower_mems_np[i].keys():
        #     for m, m_np in zip(tower_mems[i][key], tower_mems_np[i][key]):
        #       feed_dict[m] = m_np

        fetched = sess.run(fetches)

        loss_np, curr_step, gnorm_np, learning_rate_np, _, summary_np, inputs_np, hidden_states_np, special_np = fetched
        total_loss += loss_np

        #########
        ##### PYTORCH

        # f_inp = torch.tensor(inputs_np["input_ids"], dtype=torch.long, device=device)
        # f_seg_id = torch.tensor(inputs_np["segment_ids"], dtype=torch.long, device=device)
        # f_inp_mask = torch.tensor(inputs_np["input_mask"], dtype=torch.float, device=device)
        # f_label = torch.tensor(inputs_np["label_ids"], dtype=torch.float, device=device)

        # with torch.no_grad():
        #   _, hidden_states_pt, _ = pt_model.transformer(f_inp, f_seg_id, f_inp_mask)
        # logits_pt, _ = pt_model(f_inp, token_type_ids=f_seg_id, input_mask=f_inp_mask)

        # pt_model.eval()  # disactivate dropout
        # outputs = pt_model(f_inp, token_type_ids=f_seg_id, input_mask=f_inp_mask, labels=f_label)
        # loss_pt = outputs[0]
        # loss_pt = loss_pt.mean()
        # total_loss_pt += loss_pt.item()

        # # hidden_states_pt = list(t.detach().cpu().numpy() for t in hidden_states_pt)
        # # special_pt = special_pt.detach().cpu().numpy()

        # # Optimizer pt
        # pt_model.zero_grad()
        # loss_pt.backward()
        # gnorm_pt = torch.nn.utils.clip_grad_norm_(pt_model.parameters(), FLAGS.clip)
        # for param_group in optimizer.param_groups:
        #     param_group['lr'] = learning_rate_np
        # optimizer.step()

        ##### PYTORCH
        #########

        if curr_step > 0 and curr_step % FLAGS.iterations == 0:
          curr_loss = total_loss / (curr_step - prev_step)
          curr_loss_pt = total_loss_pt / (curr_step - prev_step)
          tf.logging.info("[{}] | gnorm {:.2f} lr {:8.6f} "
              "| loss {:.2f} | pplx {:>7.2f}, bpc {:>7.4f}".format(
              curr_step, gnorm_np, learning_rate_np,
              curr_loss, math.exp(curr_loss), curr_loss / math.log(2)))

          tf.logging.info("[{}] | gnorm {:.2f} lr {:8.6f} "
              "| loss PT {:.2f} | pplx PT {:>7.2f}, bpc {:>7.4f}".format(
              curr_step, gnorm_pt, learning_rate_np,
              curr_loss_pt, math.exp(curr_loss_pt), curr_loss_pt / math.log(2)))

          total_loss, total_loss_pt, prev_step = 0., 0., curr_step
          writer.add_summary(summary_np, global_step=curr_step)

        if curr_step > 0 and curr_step % FLAGS.save_steps == 0:
          save_path = os.path.join(FLAGS.model_dir, "model.ckpt-{}".format(curr_step))
          saver.save(sess, save_path)
          tf.logging.info("Model saved in path: {}".format(save_path))

          # Save a trained model, configuration and tokenizer
          model_to_save = pt_model.module if hasattr(pt_model, 'module') else pt_model  # Only save the model it-self
          # If we save using the predefined names, we can load using `from_pretrained`
          output_dir = os.path.join(FLAGS.output_dir, "pytorch-ckpt-{}".format(curr_step))
          if not tf.gfile.Exists(output_dir):
            tf.gfile.MakeDirs(output_dir)
          output_model_file = os.path.join(output_dir, WEIGHTS_NAME)
          output_config_file = os.path.join(output_dir, CONFIG_NAME)

        #########
        ##### PYTORCH
        #   torch.save(model_to_save.state_dict(), output_model_file)
        #   model_to_save.config.to_json_file(output_config_file)
        #   tokenizer.save_vocabulary(output_dir)
        #   tf.logging.info("PyTorch Model saved in path: {}".format(output_dir))
        ##### PYTORCH
        #########

        if curr_step >= FLAGS.train_steps:
          break

if __name__ == "__main__":
  tf.app.run()
