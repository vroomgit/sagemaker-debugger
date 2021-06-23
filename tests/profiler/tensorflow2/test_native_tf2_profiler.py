# Standard Library
import json
import os
import time
from datetime import datetime
from pathlib import Path

# Third Party
import pytest
import tensorflow as tf
from tests.profiler.core.utils import validate_python_profiling_stats
from tests.profiler.tensorflow2.utils import verify_detailed_profiling
from tests.tensorflow2.utils import ModelType

# First Party
import smdebug.tensorflow as smd
from smdebug.core.collection import CollectionKeys
from smdebug.core.utils import FRAMEWORK
from smdebug.profiler.profiler_config_parser import ProfilerConfigParser
from smdebug.profiler.profiler_constants import (
    CONVERT_TO_MICROSECS,
    CPROFILE_NAME,
    DEFAULT_PREFIX,
    MODELTIMELINE_SUFFIX,
    PYINSTRUMENT_NAME,
    TENSORBOARDTIMELINE_SUFFIX,
    TRACE_DIRECTORY_FORMAT,
)
from smdebug.profiler.python_profile_utils import StepPhase
from smdebug.tensorflow import KerasHook as Hook


@pytest.fixture
def native_tf2_cprofile_profiler_config_parser(config_folder, monkeypatch):
    config_path = os.path.join(
        config_folder, "test_native_tf2_cprofile_profiler_config_parser.json"
    )
    monkeypatch.setenv("SMPROFILER_CONFIG_PATH", config_path)
    return ProfilerConfigParser(FRAMEWORK.TENSORFLOW)


@pytest.fixture
def native_tf2_pyinstrument_profiler_config_parser(config_folder, monkeypatch):
    config_path = os.path.join(
        config_folder, "test_native_tf2_pyinstrument_profiler_config_parser.json"
    )
    monkeypatch.setenv("SMPROFILER_CONFIG_PATH", config_path)
    return ProfilerConfigParser(FRAMEWORK.TENSORFLOW)


def _helper_native_tf2_gradtape(hook, model, opt, dataset, profiler_config_parser, strategy=None):
    def get_grads(images, labels):
        return model(images, training=True)

    def train_step(images, labels):
        with hook.profiler():
            labels = tf.one_hot(labels, depth=10)
            with tf.GradientTape() as tape:
                logits = tf.reduce_mean(get_grads(images, labels))
                if start_step <= hook.step < end_step:
                    assert profiler_config_parser.python_profiler._start_step == hook.step
                    assert (
                        profiler_config_parser.python_profiler._start_phase == StepPhase.STEP_START
                    )
            grads = tape.gradient(logits, model.variables)
            opt.apply_gradients(zip(grads, model.variables))

        if start_step <= hook.step < end_step:
            assert profiler_config_parser.python_profiler._start_step == hook.step
            assert profiler_config_parser.python_profiler._start_phase == StepPhase.STEP_END

        return logits

    @tf.function
    def distributed_train_step(images, labels):
        per_replica_losses = strategy.run(train_step, args=(images, labels))
        return strategy.reduce(tf.distribute.ReduceOp.SUM, per_replica_losses, axis=None)

    # Known issue where logging in a python callback function (i.e. atexit) during pytest causes logging errors.
    # See https://github.com/pytest-dev/pytest/issues/5502 for more information.
    hook.logger.disabled = True
    hook.profiler_config_parser = profiler_config_parser

    start_step = profiler_config_parser.config.python_profiling_config.start_step
    end_step = start_step + profiler_config_parser.config.python_profiling_config.num_steps

    for current_step, (data, labels) in enumerate(dataset):
        logits = distributed_train_step(data, labels)
        hook.save_tensor("inputs", data, CollectionKeys.INPUTS)
        hook.save_tensor("logits", logits, CollectionKeys.OUTPUTS)
        hook.save_tensor("labels", labels, CollectionKeys.OUTPUTS)

    # required for these tests since this normally gets called in the cleanup process and we need to stop any ongoing
    # profiling and collect post-hook-close Python profiling stats
    hook.profiling_end()


def _verify_tensor_names(out_dir):
    """
    This verifies the tensor names when debugger is enabled.
    """

    trial = smd.create_trial(out_dir)
    assert len(trial.steps()) > 0, "Nothing saved at any step."
    assert len(trial.tensor_names()) > 0, "Tensors were not saved."
    assert trial.tensor_names(collection=CollectionKeys.LOSSES) == ["loss"]
    assert len(trial.tensor_names(collection=CollectionKeys.WEIGHTS)) > 0
    assert len(trial.tensor_names(collection=CollectionKeys.BIASES)) > 0
    assert trial.tensor_names(collection="optimizer_variables") == [
        "Adam/beta_1:0",
        "Adam/beta_2:0",
        "Adam/decay:0",
        "Adam/iter:0",
        "Adam/learning_rate:0",
    ]
    assert trial.tensor_names(collection=CollectionKeys.INPUTS) == ["inputs"]
    assert trial.tensor_names(collection=CollectionKeys.OUTPUTS) == ["labels", "logits"]


def _verify_timeline_files(out_dir):
    """
    This verifies the creation of the timeline files according to file path specification.
    It reads backs the file contents to make sure it is in valid JSON format.
    """
    files = list(Path(os.path.join(out_dir, DEFAULT_PREFIX)).rglob("*.json"))

    assert len(files) == 1

    file = files[0]
    file_ts = file.name.split("_")[0]
    folder_name = file.parent.name
    assert folder_name == time.strftime(
        TRACE_DIRECTORY_FORMAT, time.gmtime(int(file_ts) / CONVERT_TO_MICROSECS)
    )
    assert folder_name == datetime.strptime(folder_name, TRACE_DIRECTORY_FORMAT).strftime(
        TRACE_DIRECTORY_FORMAT
    )

    with open(file) as timeline_file:
        events_dict = json.load(timeline_file)

    assert events_dict is not None


@pytest.mark.parametrize("python_profiler_name", [CPROFILE_NAME, PYINSTRUMENT_NAME])
@pytest.mark.parametrize(
    "model_type", [ModelType.SEQUENTIAL, ModelType.FUNCTIONAL, ModelType.SUBCLASSED]
)
@pytest.mark.parametrize("use_mirrored_strategy", [True])
def test_native_tf2_profiling(
    python_profiler_name,
    model_type,
    use_mirrored_strategy,
    get_model,
    native_tf2_cprofile_profiler_config_parser,
    native_tf2_pyinstrument_profiler_config_parser,
    out_dir,
    mnist_dataset,
    tf_eager_mode,
):
    """
    Enable all types of profiling and validate the output artfacts. Parametrizes on the type of Python
    profiler used for Python profiling as well as the model used for training.

    We cannot test dataloader profiling in pytest, because the resource config needs to be configured at
    /opt/ml/input/config/resourceconfig.json before tensorflow is even imported.
    """
    if python_profiler_name == CPROFILE_NAME:
        profiler_config_parser = native_tf2_cprofile_profiler_config_parser
    else:
        profiler_config_parser = native_tf2_pyinstrument_profiler_config_parser

    assert profiler_config_parser.profiling_enabled
    profiler_config_parser.load_config()
    profiler_config_parser.start_pre_step_zero_python_profiling()

    hook = Hook(out_dir=out_dir, save_all=True)
    strategy = None

    if use_mirrored_strategy:
        strategy = tf.distribute.MirroredStrategy()
        with strategy.scope():
            model = get_model(model_type)
            optimizer = tf.optimizers.Adam()
            optimizer = hook.wrap_optimizer(optimizer)
    else:
        model = get_model(model_type)
        optimizer = tf.optimizers.Adam()
        optimizer = hook.wrap_optimizer(optimizer)

    _helper_native_tf2_gradtape(
        hook, model, optimizer, mnist_dataset, profiler_config_parser, strategy=strategy
    )

    # Sanity check debugger output.
    # TODO: Figure out why tensors cannot be collected when both MirroredStrategy and GradientTape are used.
    if not use_mirrored_strategy:
        _verify_tensor_names(out_dir)

    # Validate all timeline files
    _verify_timeline_files(out_dir)

    # Validate detailed profiling
    expected_event_count = 90 if use_mirrored_strategy else 230
    verify_detailed_profiling(out_dir, expected_event_count)

    # The expected number of stats directories during is (num_steps * 2) + 2. This includes profiling for both
    # phases of each step and pre-step zero python profiling and post-hook-close python profiling.
    expected_stats_dir_count = (
        profiler_config_parser.config.python_profiling_config.num_steps * 2
    ) + 2
    python_stats_dir = os.path.join(out_dir, "framework", "tensorflow", python_profiler_name)
    validate_python_profiling_stats(
        python_stats_dir, python_profiler_name, expected_stats_dir_count
    )
