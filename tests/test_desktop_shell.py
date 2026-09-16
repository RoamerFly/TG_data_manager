"""Desktop pywebview bridge regression tests."""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import server  # noqa: E402
from server import DesktopWindowApi, _open_desktop_window  # noqa: E402


class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self):
        for handler in self.handlers:
            handler()


class FakeEvents:
    def __init__(self):
        self.maximized = FakeEvent()
        self.restored = FakeEvent()


class FakeWindow:
    def __init__(self):
        self.calls = []
        self.events = FakeEvents()

    def minimize(self):
        self.calls.append('minimize')

    def maximize(self):
        self.calls.append('maximize')

    def restore(self):
        self.calls.append('restore')

    def destroy(self):
        self.calls.append('destroy')


class FakeWebview:
    def __init__(self):
        self.window = FakeWindow()
        self.args = None
        self.kwargs = None
        self.started = False

    def create_window(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        return self.window

    def start(self):
        self.started = True


def check(name, condition):
    if not condition:
        raise AssertionError(name)
    print(f'  PASS  {name}')


def main():
    api = DesktopWindowApi()
    check('bridge state is private', all(k.startswith('_') for k in vars(api)))

    window = FakeWindow()
    api._bind(window)
    check('native window remains private', 'window' not in vars(api))
    check('minimize command succeeds', api.window_minimize() is True)
    bounds_calls = []
    original_set_bounds = server._set_maximized_bounds
    server._set_maximized_bounds = lambda target: bounds_calls.append(target)
    check('maximize command toggles on', api.window_toggle_maximize() is True)
    check('maximize command toggles off', api.window_toggle_maximize() is False)
    server._set_maximized_bounds = original_set_bounds
    check('maximize is constrained to the working area', bounds_calls == [window])
    check('close command succeeds', api.window_close() is True)
    check('window commands were forwarded', window.calls == [
        'minimize', 'maximize', 'restore', 'destroy'])

    webview = FakeWebview()
    _open_desktop_window(webview, 'http://127.0.0.1:5000')
    check('desktop URL marker is applied',
          webview.args is not None
          and webview.args[1] == 'http://127.0.0.1:5000/?desktop=1')
    check('window opens centered and not fullscreen',
          webview.kwargs.get('x') is None
          and webview.kwargs.get('y') is None
          and webview.kwargs.get('fullscreen') is False
          and webview.kwargs.get('maximized') is False)
    check('native minimum width matches the UI layout',
          webview.kwargs.get('min_size') == (1180, 680))
    check('webview event loop starts', webview.started)
    check('created bridge has no public state', all(
        k.startswith('_') for k in vars(webview.kwargs['js_api'])))
    shell_api = webview.kwargs['js_api']
    webview.window.events.maximized.fire()
    check('native maximize event synchronizes bridge state', shell_api._maximized)
    webview.window.events.restored.fire()
    check('native restore event synchronizes bridge state', not shell_api._maximized)

    print('\n===== 15 passed, 0 failed =====')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
