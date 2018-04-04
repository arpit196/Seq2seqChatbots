from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# tensor2tensor imports
from tensor2tensor.models import lstm
from tensor2tensor.layers import common_layers
from tensor2tensor.utils import t2t_model
from tensor2tensor.utils import registry
from tensor2tensor.utils import optimize

# tensorflow imports
import tensorflow as tf
import math

# my imports
from t2t_csaky.hparams import seq2seq_hparams
from t2t_csaky.utils import optimizer

# Flags
FLAGS = tf.flags.FLAGS


def lstm(inputs, hparams, train, name, initial_state=None):
  """Run LSTM cell on inputs, assuming they are [batch x time x size]."""

  def dropout_lstm_cell():
    return tf.contrib.rnn.DropoutWrapper(
        tf.contrib.cudnn_rnn.CudnnCompatibleLSTMCell(hparams.hidden_size),
        input_keep_prob=1.0 - hparams.dropout * tf.to_float(train))
    
  layers = [dropout_lstm_cell() for _ in range(hparams.num_hidden_layers)]
  with tf.variable_scope(name):
    return tf.nn.dynamic_rnn(
        tf.contrib.rnn.MultiRNNCell(layers),
        inputs,
        initial_state=initial_state,
        dtype=tf.float32,
        time_major=False)


def lstm_seq2seq_internal(inputs, targets, hparams, train):
  """The basic LSTM seq2seq model, main step used for training."""
  with tf.variable_scope("lstm_seq2seq"):
    if inputs is not None:
      # Flatten inputs.
      inputs = common_layers.flatten4d3d(inputs)
      # LSTM encoder.
      _, final_encoder_state = lstm(
          tf.reverse(inputs, axis=[1]), hparams, train, "encoder")
    else:
      final_encoder_state = None
    # LSTM decoder.
    shifted_targets = common_layers.shift_right(targets)
    decoder_outputs, _ = lstm(
        common_layers.flatten4d3d(shifted_targets),
        hparams,
        train,
        "decoder",
        initial_state=final_encoder_state)

    # project the outputs
    with tf.variable_scope("projection"):
      projected_outputs=tf.layers.dense(
          decoder_outputs,
          2048,
          activation=None,
          use_bias=False)
      
    return tf.expand_dims(projected_outputs, axis=2)


@registry.register_model
class GradientCheckpointedSeq2seq(t2t_model.T2TModel):
  """
  A class where I replaced the internal hparams with my own function call.
  This way the hidden_size param of chatbot_lstm_hparams refers to the hidden size
    of the lstm cells, while the hidden_size specified by the hparam set that is
    given during training refers to the word embedding size.

  Moreover, in this class gradient checkpointed is implemented.
  https://github.com/openai/gradient-checkpointing
  """
  def body(self,features):
    if self._hparams.initializer == "orthogonal":
      raise ValueError("LSTM models fail with orthogonal initializer.")
    train=self._hparams.mode==tf.estimator.ModeKeys.TRAIN
    return lstm_seq2seq_internal(
      features.get("inputs"),features["targets"],seq2seq_hparams.chatbot_lstm_hparams(),train)

  # Change the optimizer to a new one, which uses gradient checkpointing
  def optimize(self, loss, num_async_replicas=1):
    """Return a training op minimizing loss."""
    tf.logging.info("Base learning rate: %f", self.hparams.learning_rate)
    lr = self.hparams.learning_rate
    decay_rate = optimize.learning_rate_schedule(self.hparams)
    lr *= decay_rate
    if self.hparams.learning_rate_minimum:
      lr_min = float(self.hparams.learning_rate_minimum)
      tf.logging.info("Applying learning rate minimum: %f", lr_min)
      lr = tf.max(lr, tf.to_float(lr_min))
    if num_async_replicas > 1:
      tf.logging.info("Dividing learning rate by num_async_replicas: %d",
                      num_async_replicas)
    lr /= math.sqrt(float(num_async_replicas))
    train_op = optimizer.optimize(loss, lr, self.hparams)
    return train_op