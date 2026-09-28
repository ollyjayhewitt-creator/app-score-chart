import os
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import darktask as dt  # noqa: E402
from pynput import keyboard, mouse  # noqa: E402


class FakeMouse:
    def __init__(self):
        self.log = []
        self._pos = (0, 0)

    @property
    def position(self):
        return self._pos

    @position.setter
    def position(self, pos):
        self._pos = tuple(pos)
        self.log.append(("move",) + self._pos)

    def press(self, button):
        self.log.append(("press", button.name))

    def release(self, button):
        self.log.append(("release", button.name))

    def scroll(self, dx, dy):
        self.log.append(("scroll", dx, dy))


class FakeKeyboard:
    def __init__(self, fail_on=None):
        self.log = []
        self.fail_on = fail_on

    def press(self, key):
        if key == self.fail_on:
            raise ValueError("can't type that")
        self.log.append(("press", key))

    def release(self, key):
        self.log.append(("release", key))


def play(macro, **kwargs):
    m, k = FakeMouse(), FakeKeyboard(kwargs.pop("fail_on", None))
    done = threading.Event()
    player = dt.Player(macro, mouse_ctl=m, keyboard_ctl=k, on_finish=lambda p: done.set(), **kwargs)
    player.start()
    return player, m, k, done


class KeyTests(unittest.TestCase):
    def test_special_key_round_trip(self):
        data = dt.key_to_data(keyboard.Key.shift)
        self.assertEqual(data, {"name": "shift"})
        self.assertEqual(dt.data_to_key(data), keyboard.Key.shift)

    def test_char_key_round_trip(self):
        data = dt.key_to_data(keyboard.KeyCode(vk=65, char="a"))
        self.assertEqual(data, {"vk": 65, "char": "a"})
        self.assertEqual(dt.data_to_key(data).vk, 65)
        self.assertEqual(dt.data_to_key(data, prefer_char=True).char, "a")
        self.assertEqual(dt.data_to_key({"char": "z"}).char, "z")

    def test_unknown_key_name_is_skipped(self):
        self.assertIsNone(dt.data_to_key({"name": "no_such_key"}))
        self.assertIsNone(dt.data_to_key({}))

    def test_key_matches(self):
        self.assertTrue(dt.key_matches(keyboard.Key.f8, {"name": "f8"}))
        self.assertFalse(dt.key_matches(keyboard.Key.f9, {"name": "f8"}))
        self.assertTrue(dt.key_matches(keyboard.KeyCode(vk=81, char="q"), {"vk": 81, "char": "q"}))
        self.assertTrue(dt.key_matches(keyboard.KeyCode(char="Q"), {"char": "q"}))
        self.assertFalse(dt.key_matches(keyboard.Key.f8, {"char": "q"}))
        self.assertFalse(dt.key_matches(None, {"name": "f8"}))

    def test_labels_and_modifiers(self):
        self.assertEqual(dt.key_label({"name": "f8"}), "F8")
        self.assertEqual(dt.key_label({"name": "page_down"}), "Page Down")
        self.assertEqual(dt.key_label({"vk": 81, "char": "q"}), "Q")
        self.assertTrue(dt.is_modifier({"name": "ctrl_l"}))
        self.assertFalse(dt.is_modifier({"name": "f8"}))


class MacroTests(unittest.TestCase):
    EVENTS = [
        [0.1, "move", 10, 20],
        [0.2, "click", 10, 20, "left", True],
        [0.3, "click", 10, 20, "left", False],
        [0.4, "key", {"vk": 72, "char": "h"}, True],
        [0.5, "key", {"vk": 72, "char": "h"}, False],
        [0.6, "scroll", 10, 20, 0, -1],
    ]

    def test_save_and_load(self):
        macro = dt.Macro([list(e) for e in self.EVENTS], duration=2.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "m.json")
            macro.save(path)
            loaded = dt.Macro.load(path)
        self.assertEqual(loaded.events, self.EVENTS)
        self.assertEqual(loaded.duration, 2.0)
        self.assertEqual(loaded.platform, sys.platform)

    def test_bad_events_are_dropped_and_sorted(self):
        macro = dt.Macro.from_dict({"events": [
            [0.5, "move", 1, 2], "junk", [0.2, "click", 1], [0.1, "move", "x", 2],
            [0.3, "teleport", 1, 2], [0.1, "move", 5, 6]]})
        self.assertEqual(macro.events, [[0.1, "move", 5, 6], [0.5, "move", 1, 2]])
        self.assertEqual(macro.duration, 0.5)

    def test_counts_and_summary(self):
        macro = dt.Macro([list(e) for e in self.EVENTS])
        self.assertEqual(macro.counts(), {"move": 1, "click": 1, "scroll": 1, "key": 1})
        self.assertIn("1 click", macro.summary())
        self.assertEqual(dt.Macro().summary(), "No macro yet")

    def test_fmt_duration(self):
        self.assertEqual(dt.fmt_duration(4.25), "4.2s")
        self.assertEqual(dt.fmt_duration(125), "2:05")
        self.assertEqual(dt.fmt_duration(3725), "1:02:05")


class RecorderTests(unittest.TestCase):
    def make(self, **kwargs):
        rec = dt.Recorder(**kwargs)
        rec._begin()
        return rec

    def test_records_everything_in_order(self):
        rec = self.make()
        rec._on_move(5, 5)
        rec._on_move(5, 5)  # duplicate position is dropped
        rec._on_click(5, 5, mouse.Button.left, True)
        rec._on_click(5, 5, mouse.Button.left, False)
        rec._on_scroll(5, 5, 0, 2)
        rec._on_press(keyboard.Key.space)
        rec._on_release(keyboard.Key.space)
        kinds = [e[1] for e in rec.stop().events]
        self.assertEqual(kinds, ["move", "click", "click", "scroll", "key", "key"])

    def test_ignores_hotkeys_injected_and_orphan_releases(self):
        rec = self.make(ignore_key=lambda k: k == keyboard.Key.f8)
        rec._on_press(keyboard.Key.f8)
        rec._on_release(keyboard.Key.f8)
        rec._on_press(keyboard.Key.enter, True)          # injected
        rec._on_move(1, 1, True)                          # injected
        rec._on_release(keyboard.Key.tab)                 # pressed before recording
        rec._on_click(1, 1, mouse.Button.left, False)     # pressed before recording
        self.assertTrue(rec.stop().empty)

    def test_clicks_on_own_window_are_skipped(self):
        rec = self.make(ignore_point=lambda x, y: x < 100)
        rec._on_click(50, 50, mouse.Button.left, True)
        rec._on_click(500, 50, mouse.Button.left, False)  # release of the ignored press
        rec._on_scroll(50, 50, 0, 1)
        rec._on_click(500, 50, mouse.Button.right, True)
        rec._on_click(500, 50, mouse.Button.right, False)
        events = rec.stop().events
        self.assertEqual([e[4] for e in events], ["right", "right"])

    def test_held_keys_and_buttons_are_released_at_stop(self):
        rec = self.make()
        rec._on_press(keyboard.Key.ctrl_l)
        rec._on_click(9, 9, mouse.Button.left, True)
        macro = rec.stop()
        tail = macro.events[-2:]
        self.assertEqual(tail[0][1:], ["click", 9, 9, "left", False])
        self.assertEqual(tail[1][1:], ["key", dt.key_to_data(keyboard.Key.ctrl_l), False])

    def test_mouse_movement_can_be_off(self):
        rec = self.make(record_moves=False)
        rec._on_move(1, 2)
        rec._on_click(1, 2, mouse.Button.left, True)
        self.assertEqual([e[1] for e in rec.stop().events], ["click", "click"])


class PlayerTests(unittest.TestCase):
    def test_replays_in_order(self):
        macro = dt.Macro(MacroTests.EVENTS, duration=0.7)
        player, m, k, done = play(macro, speed=10)
        self.assertTrue(done.wait(2))
        self.assertEqual(m.log, [("move", 10, 20), ("move", 10, 20), ("press", "left"),
                                 ("move", 10, 20), ("release", "left"),
                                 ("move", 10, 20), ("scroll", 0, -1)])
        self.assertEqual([action for action, _ in k.log], ["press", "release"])
        self.assertIsNone(player.error)

    def test_repeat_and_speed(self):
        macro = dt.Macro([[0.2, "move", 1, 1]], duration=0.4)
        start = time.perf_counter()
        player, m, _, done = play(macro, speed=4, repeat=3)
        self.assertTrue(done.wait(2))
        elapsed = time.perf_counter() - start
        self.assertEqual(player.loop, 3)
        self.assertEqual(len(m.log), 3)
        self.assertGreater(elapsed, 0.25)   # 3 loops x 0.1s
        self.assertLess(elapsed, 0.9)       # far less than 3 x 0.4s at 1x

    def test_loop_delay(self):
        macro = dt.Macro([[0.0, "move", 1, 1]], duration=0.0)
        start = time.perf_counter()
        _, m, _, done = play(macro, repeat=3, loop_delay=0.15)
        self.assertTrue(done.wait(2))
        self.assertEqual(len(m.log), 3)
        self.assertGreater(time.perf_counter() - start, 0.28)  # two gaps, none after the last

    def test_stop_releases_held_keys(self):
        macro = dt.Macro([
            [0.0, "key", {"name": "shift"}, True],
            [0.0, "click", 3, 3, "left", True],
            [5.0, "key", {"name": "shift"}, False],
        ])
        player, m, k, done = play(macro, repeat=0)
        time.sleep(0.2)
        player.stop()
        self.assertTrue(done.wait(1))
        self.assertIn(("release", keyboard.Key.shift), k.log)
        self.assertIn(("release", "left"), m.log)
        self.assertEqual(player.loop, 1)

    def test_forever_until_stopped(self):
        macro = dt.Macro([[0.0, "move", 1, 1]], duration=0.02)
        player, m, _, done = play(macro, repeat=0)
        time.sleep(0.3)
        self.assertFalse(done.is_set())
        player.stop()
        self.assertTrue(done.wait(1))
        self.assertGreater(player.loop, 3)

    def test_unplayable_steps_are_skipped(self):
        macro = dt.Macro([
            [0.0, "key", {"name": "space"}, True],
            [0.0, "key", {"name": "no_such_key"}, True],
            [0.0, "click", 1, 1, "no_such_button", True],
            [0.0, "move", 4, 4],
        ])
        player, m, _, done = play(macro, fail_on=keyboard.Key.space)
        self.assertTrue(done.wait(1))
        self.assertEqual(player.skipped, 1)
        self.assertEqual(m.log, [("move", 4, 4)])
        self.assertIsNone(player.error)


class AppSmokeTest(unittest.TestCase):
    def test_window_builds_and_toggles(self):
        try:
            import tkinter as tk
            root = tk.Tk()
        except Exception as exc:  # no display available
            self.skipTest("no display: %s" % exc)
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(dt, "config_dir", return_value=tmp):
            root.withdraw()
            app = dt.App(root)
            try:
                app.toggle_options()
                app.toggle_options()
                app._set_speed(4)
                self.assertEqual(app.settings["speed"], 4.0)
                app.macro = dt.Macro([[0.0, "move", 1, 1]])
                app._refresh()
                self.assertTrue(app.play_btn._enabled)
                app.toggle_record()
                self.assertEqual(app.state, dt.RECORDING)
                app.toggle_record()
                self.assertEqual(app.state, dt.IDLE)
                self.assertFalse(app.macro.empty)  # empty recording kept the old macro
            finally:
                app.quit()
            self.assertTrue(os.path.exists(os.path.join(tmp, "settings.json")))


if __name__ == "__main__":
    unittest.main()
