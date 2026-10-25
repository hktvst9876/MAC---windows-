"""Settings transactions and audio-device selection without real sound devices."""

import dataclasses
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from mirrorbridge.config import AppConfig
from mirrorbridge.media import audio
from mirrorbridge.ui import main_window, settings_dialog


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


@pytest.fixture
def fake_sounddevice(monkeypatch):
    devices = [
        {"name": "Speakers", "hostapi": 0, "max_output_channels": 2, "max_input_channels": 0},
        {"name": "Speakers", "hostapi": 1, "max_output_channels": 2, "max_input_channels": 0},
        {"name": "Microphone", "hostapi": 0, "max_output_channels": 0, "max_input_channels": 2},
        {"name": "Microphone", "hostapi": 1, "max_output_channels": 0, "max_input_channels": 2},
    ]
    backend = SimpleNamespace(
        devices=devices,
        query_devices=lambda: devices,
        query_hostapis=lambda: [{"name": "MME"}, {"name": "WASAPI"}],
        OutputStream=Mock(return_value=Mock()),
    )
    monkeypatch.setattr(audio, "sd", backend)
    return backend


@pytest.mark.parametrize("accept_restart", [False, True])
def test_settings_commit_only_after_restart_confirmation(monkeypatch, accept_restart):
    config = AppConfig(password="old password")
    before = dataclasses.asdict(config)
    saved = []
    monkeypatch.setattr(AppConfig, "save", lambda self: saved.append(dataclasses.asdict(self)))

    class EditedDialog:
        def __init__(self, candidate, parent):
            assert candidate is not config
            self.config = candidate

        def exec(self):
            return True

        def apply(self):
            self.config.password = "new password"
            self.config.audio_enabled = False
            self.config.show_stats = True
            return True

    monkeypatch.setattr(main_window, "SettingsDialog", EditedDialog)
    monkeypatch.setattr(main_window.QMessageBox, "question", lambda *args:
                        main_window.QMessageBox.Yes if accept_restart else main_window.QMessageBox.No)
    controller = SimpleNamespace(config=config, restart=Mock())
    stats_action = Mock()
    mirror = SimpleNamespace(video=SimpleNamespace(show_stats=False), act_stats=stats_action)
    window = SimpleNamespace(config=config, rows={"connected": object()},
                             mirror_windows={"connected": mirror}, controller=controller, _refresh=Mock())

    main_window.MainWindow.open_settings(window)

    assert window.config is controller.config is config
    if accept_restart:
        assert config.password == "new password" and not config.audio_enabled
        assert saved == [dataclasses.asdict(config)]
        controller.restart.assert_called_once_with()
        assert mirror.video.show_stats
        stats_action.setChecked.assert_called_once_with(True)
        window._refresh.assert_called_once_with()
    else:
        assert dataclasses.asdict(config) == before
        assert not saved
        controller.restart.assert_not_called()
        stats_action.setChecked.assert_not_called()
        window._refresh.assert_not_called()


@pytest.mark.parametrize("change", ["disable_audio", "change_output", "change_decoder"])
def test_existing_stream_settings_require_restart(qapp, fake_sounddevice, change):
    dialog = settings_dialog.SettingsDialog(AppConfig())
    try:
        if change == "disable_audio":
            dialog.audio.setChecked(False)
        elif change == "change_output":
            dialog.audio_device.setCurrentIndex(1)
        else:
            dialog.hw.setCurrentIndex(dialog.hw.findData("off"))
        assert dialog.apply()
    finally:
        dialog.close()


def test_device_ids_disambiguate_host_apis_and_survive_reordering(fake_sounddevice):
    outputs = audio.output_devices()
    inputs = audio.input_devices()
    assert outputs[0][0] != outputs[1][0]
    assert [label for _, label in outputs] == ["Speakers (MME)", "Speakers (WASAPI)"]
    assert [label for _, label in inputs] == ["Microphone (MME)", "Microphone (WASAPI)"]
    selected_output = outputs[1][0]
    selected_input = inputs[1][0]
    assert audio.resolve_device(selected_output, "output") == 1
    assert audio.resolve_device(selected_input, "input") == 3

    fake_sounddevice.devices.reverse()
    assert audio.resolve_device(selected_output, "output") == 2
    assert audio.resolve_device(selected_input, "input") == 0
    player = audio.AudioPlayer(device=selected_output)
    try:
        assert player.available
        assert fake_sounddevice.OutputStream.call_args.kwargs["device"] == 2
    finally:
        player.close()


def test_missing_selected_device_does_not_fall_back_to_another_host_api(fake_sounddevice):
    selected = audio.output_devices()[1][0]
    fake_sounddevice.devices.pop(1)
    with pytest.raises(ValueError, match="not available"):
        audio.resolve_device(selected, "output")


def test_settings_save_the_selected_output_and_microphone_id(qapp, fake_sounddevice):
    config = AppConfig()
    dialog = settings_dialog.SettingsDialog(config)
    try:
        dialog.audio_device.setCurrentIndex(2)
        dialog.mic_device.setCurrentIndex(2)
        assert dialog.apply()
        assert audio.resolve_device(config.audio_device, "output") == 1
        assert audio.resolve_device(config.mic_device, "input") == 3
    finally:
        dialog.close()


@pytest.mark.parametrize("device", [None, "", "Speakers", 1])
def test_legacy_and_default_device_values_remain_supported(device):
    assert audio.resolve_device(device, "output") == (None if device == "" else device)


def test_settings_preserve_legacy_device_names(qapp, fake_sounddevice):
    config = AppConfig(audio_device="Speakers", mic_device="Microphone")
    dialog = settings_dialog.SettingsDialog(config)
    try:
        assert dialog.audio_device.currentData() == "Speakers"
        assert dialog.mic_device.currentData() == "Microphone"
        assert not dialog.apply()
        assert config.audio_device == "Speakers" and config.mic_device == "Microphone"
    finally:
        dialog.close()
