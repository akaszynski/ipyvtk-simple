"""Originally implemented by Andras Lasso under an MIT License


Source:
    https://github.com/Slicer/SlicerJupyter

    https://github.com/Slicer/SlicerJupyter/blob/master/JupyterNotebooks/JupyterNotebooksLib/interactive_view_widget.py

"""
import time
import logging
import weakref
from io import BytesIO
import PIL.Image

from ipycanvas import Canvas
from ipyevents import Event
import numpy as np
from ipywidgets import Image

from .constants import INTERACTION_THROTTLE, KEY_TO_SYM
from .throttler import throttle


log = logging.getLogger(__name__)
log.setLevel("CRITICAL")
log.addHandler(logging.StreamHandler())


class ViewInteractiveWidget(Canvas):
    """Remote controller for VTK render windows.

    Parameters
    ----------
    quality : float
        Compression quality.  100 for best quality, 0 for min quality.
        Default 80.

    on_close : callable
        A callable function with no aruments to be triggered when the widget
        is destroyed. This is useful to have a callback to close/clean up the
        render window.

    """

    def __init__(self, render_window, log_events=True,
                 transparent_background=False, allow_wheel=True, quality=80,
                 on_close=None, **kwargs):
        """Accepts a vtkRenderWindow."""

        super().__init__(**kwargs)
        if quality < 0 or quality > 100:
            raise ValueError('`quality` parameter must be between 0 and 100')
        self._quality = quality
        self._render_window = weakref.ref(render_window)
        self.render_window.SetOffScreenRendering(1)  # Force off screen
        self.transparent_background = transparent_background

        # track mobile touch start/end
        self._touching = False
        self._last_touch = time.time()

        # Frame rate (1/renderDelay)
        self.last_render_time = 0
        self.quick_render_delay_sec = 0.001
        self.quick_render_delay_sec_range = [0.001, 2.0]
        self.adaptive_render_delay = True
        self.last_mouse_move_event = None
        self._has_moved = False

        # refresh if mouse is just moving (not dragging)
        self.track_mouse_move = False

        self.message_timestamp_offset = None

        self.layout.width = '100%'
        self.layout.height = 'auto'

        # Set Canvas size from window size
        self.width, self.height = self.render_window.GetSize()

        # record first render time
        tstart = time.time()
        self.update_canvas()
        self._first_render_time = time.time() - tstart
        log.debug('First image in %.5f seconds', self._first_render_time)

        self.dragging = False

        self.interaction_events = Event()
        # Set the throttle or debounce time in millseconds (must be an non-negative integer)
        # See https://github.com/mwcraig/ipyevents/pull/55
        self.interaction_events.throttle_or_debounce = "throttle"
        self.interaction_events.wait = INTERACTION_THROTTLE
        self.interaction_events.source = self

        allowed_events = [
            "dragstart",
            "mouseenter",
            "mouseleave",
            "mousedown",
            "mouseup",
            "mousemove",
            "keyup",
            "keydown",
            "contextmenu",  # prevent context menu from appearing on right-click
            "touch_move"
        ]

        # May be disabled out so that user can scroll through the
        # notebook using mousewheel
        if allow_wheel:
            allowed_events.append("wheel")

        self.interaction_events.watched_events = allowed_events

        # self.interaction_events.msg_throttle = 1  # does not seem to have effect
        self.interaction_events.prevent_default_action = True
        self.interaction_events.on_dom_event(self.handle_interaction_event)

        # Errors are not displayed when a widget is displayed,
        # this variable can be used to retrieve error messages
        self.error = None

        # Enable logging of UI events
        self.log_events = log_events
        self.logged_events = []
        self.elapsed_times = []
        self.age_of_processed_messages = []

        if hasattr(on_close, '__call__'):
            self._on_close = on_close
        else:
            self._on_close = lambda: None

        # register touch callbacks
        self._stuff = []
        self.on_touch_start(self._on_touch_start)
        self.on_touch_end(self._on_touch_end)
        self.on_touch_move(self._on_touch_move)


    def _on_touch_start(self, locs):
        """Assigned to ``on_touch_start`` event"""
        self._touching = True
        # self.dragging = True
        event = {'event': 'mousedown',
                 'touch_event': True,
                 'offsetX': int(locs[0][0]),
                 'offsetY': int(locs[0][1]),
                 'timeStamp': time.time(),
                 # 'boundingRectWidth': last_event['boundingRectWidth'],
                 # 'boundingRectHeight': last_event['boundingRectHeight'],
                 "button": 0
        }
        self.handle_interaction_event(event)

    def _on_touch_end(self, *args):
        """Assigned to ``on_touch_end`` event"""
        event = {'event': 'mouseup',
                 'touch_event': True,
                 'timeStamp': time.time(),
                 # 'boundingRectWidth': last_event['boundingRectWidth'],
                 # 'boundingRectHeight': last_event['boundingRectHeight'],
                 "button": 0
        }
        self.handle_interaction_event(event)
        self._touching = False

    # @debounce(0.01)
    def _on_touch_move(self, locs):
        """create a touch event and pass that"""
        self._stuff = locs
        x, y = locs[0]  # passed as a tuple?
        # if not self.logged_events:
        #     return

        # # need ipyevents actual canvas size
        # last_event = self.logged_events[-1]
        # if 'boundingRectWidth' not in last_event:
        #     return
        if time.time() - self._last_touch < 0.01:
            return

        self._last_touch = time.time()
        event = {'event': 'mousemove',
                 'touch_event': True,
                 'offsetX': round(x),
                 'offsetY': round(y),
                 'timeStamp': time.time(),
                 # 'boundingRectWidth': last_event['boundingRectWidth'],
                 # 'boundingRectHeight': last_event['boundingRectHeight'],
        }
        self.handle_interaction_event(event)

    @property
    def render_window(self):
        """reference the weak reference"""
        ren_win = self._render_window()
        if ren_win is None:
            raise RuntimeError('VTK render window has closed')
        return ren_win

    @property
    def interactor(self):
        return self.render_window.GetInteractor()

    def set_quick_render_delay(self, delay_sec):
        if delay_sec < self.quick_render_delay_sec_range[0]:
            delay_sec = self.quick_render_delay_sec_range[0]
        elif delay_sec > self.quick_render_delay_sec_range[1]:
            delay_sec = self.quick_render_delay_sec_range[1]
        self.quick_render_delay_sec = delay_sec

    def update_canvas(self, force_render=True):
        """Updates the canvas with the current render"""
        raw_img = self.get_image(force_render=force_render)
        f = BytesIO()
        PIL.Image.fromarray(raw_img).save(f, 'JPEG', quality=self._quality)
        image = Image(
            value=f.getvalue(), width=self.width, height=self.height
        )
        self.draw_image(image)

    def get_image(self, force_render=True):
        if force_render:
            self.render_window.Render()
        return self._fast_image

    @property
    def _fast_image(self):
        import vtk.util.numpy_support as nps
        import vtk
        arr = vtk.vtkUnsignedCharArray()
        self.render_window.GetRGBACharPixelData(0, 0, self.width - 1,
                                                self.height - 1, 0, arr)

        data = nps.vtk_to_numpy(arr).reshape(self.height, self.width, -1)[::-1]

        if self.transparent_background:
            return data
        else:  # ignore alpha channel
            return data[:, :, :-1]

    @throttle(0.1)
    def full_render(self):
        try:
            import time
            tstart = time.time()
            self.update_canvas(True)
            self.last_render_time = time.time()
            log.debug('full render in %.5f seconds', time.time() - tstart)
        except Exception as e:
            self.error = str(e)

    def send_pending_mouse_move_event(self):
        if self.last_mouse_move_event is not None:
            self.update_interactor_event_data(self.last_mouse_move_event)
            self.interactor.MouseMoveEvent()
            self.last_mouse_move_event = None

    @throttle(0.01)
    def quick_render(self):
        try:
            self.send_pending_mouse_move_event()
            self.update_canvas()
            if self.log_events:
                self.elapsed_times.append(time.time() - self.last_render_time)
            self.last_render_time = time.time()
        except Exception as e:
            self.error = str(e)

    def update_interactor_event_data(self, event):
        try:
            if event["event"] == "keydown" or event["event"] == "keyup":
                key = event["key"]
                sym = KEY_TO_SYM[key] if key in KEY_TO_SYM.keys() else key
                self.interactor.SetKeySym(sym)
                if len(key) == 1:
                    self.interactor.SetKeyCode(key)
                self.interactor.SetRepeatCount(1)
            else:
                self.interactor.SetEventPosition(event["offsetX"],
                                                 self.height - event["offsetY"]
                )
            if "shiftKey" in event:
                self.interactor.SetShiftKey(event["shiftKey"])
            if "ctrlKey" in event:
                self.interactor.SetControlKey(event["ctrlKey"])
            if "altKey" in event:
                self.interactor.SetAltKey(event["altKey"])
        except Exception as e:
            self.error = str(e)

    def handle_interaction_event(self, event):
        if self.log_events:
            self.logged_events.append(event)

        event_name = event["event"]

        # we have to scale the mouse movement here relative to the
        # canvas size, otherwise mouse movement won't correspond to
        # the render window.
        if 'offsetX' in event and 'boundingRectWidth' in event:
            scale_x = self.width/event['boundingRectWidth']
            event['offsetX'] = round(event['offsetX']*scale_x)
            scale_y = self.height/event['boundingRectHeight']
            event['offsetY'] = round(event['offsetY']*scale_y)

        try:
            if self._touching and 'touch_event' not in event:
                return

            if event_name == "mousemove":
                if self.message_timestamp_offset is None:
                    self.message_timestamp_offset = (
                        time.time() - event["timeStamp"] * 0.001
                    )

                self.last_mouse_move_event = event
                if not self.dragging and not self.track_mouse_move:
                    return
                if self.adaptive_render_delay and not self._touching:
                    ageOfProcessedMessage = time.time() - (
                        event["timeStamp"] * 0.001 + self.message_timestamp_offset
                    )

                    if ageOfProcessedMessage > 1.5 * self.quick_render_delay_sec:
                        # we are falling behind, try to render less frequently
                        self.set_quick_render_delay(self.quick_render_delay_sec * 1.05)
                    elif ageOfProcessedMessage < 0.5 * self.quick_render_delay_sec:
                        # we can keep up with events, try to render more frequently
                        self.set_quick_render_delay(self.quick_render_delay_sec / 1.05)

                    if self.log_events:
                        self.age_of_processed_messages.append(
                            [ageOfProcessedMessage, self.quick_render_delay_sec]
                        )
                # We need to render something now it no rendering
                # since self.quick_render_delay_sec
                if time.time() - self.last_render_time > self.quick_render_delay_sec:
                    self.quick_render()
            elif event_name == "mouseenter":
                self.update_interactor_event_data(event)
                self.interactor.EnterEvent()
                self.last_mouse_move_event = None
            elif event_name == "mouseleave":
                self.update_interactor_event_data(event)
                self.interactor.LeaveEvent()
                self.last_mouse_move_event = None
                if self.dragging:  # have to trigger a leave event and release event
                    self.interactor.LeftButtonReleaseEvent()
                    self.dragging = False
                self.full_render()
            elif event_name == "mousedown":
                self.dragging = True
                self.send_pending_mouse_move_event()
                self.update_interactor_event_data(event)
                if event["button"] == 0:
                    self.interactor.LeftButtonPressEvent()
                elif event["button"] == 2:
                    self.interactor.RightButtonPressEvent()
                elif event["button"] == 1:
                    self.interactor.MiddleButtonPressEvent()
                self.full_render()  # does this have to be rendered?
            elif event_name == "mouseup":
                self.send_pending_mouse_move_event()
                self.update_interactor_event_data(event)
                if event["button"] == 0:
                    self.interactor.LeftButtonReleaseEvent()
                elif event["button"] == 2:
                    self.interactor.RightButtonReleaseEvent()
                elif event["button"] == 1:
                    self.interactor.MiddleButtonReleaseEvent()
                self.dragging = False
                self.full_render()
            elif event_name == "keydown":
                self.send_pending_mouse_move_event()
                self.update_interactor_event_data(event)
                self.interactor.KeyPressEvent()
                self.interactor.CharEvent()
                if (
                    event["key"] != "Shift"
                    and event["key"] != "Control"
                    and event["key"] != "Alt"
                ):
                    self.full_render()
            elif event_name == "keyup":
                self.send_pending_mouse_move_event()
                self.update_interactor_event_data(event)
                self.interactor.KeyReleaseEvent()
                if (
                    event["key"] != "Shift"
                    and event["key"] != "Control"
                    and event["key"] != "Alt"
                ):
                    self.full_render()
            elif event_name == 'wheel':
                if 'wheel' in self.interaction_events.watched_events:
                    self.send_pending_mouse_move_event()
                    self.update_interactor_event_data(event)
                    if event['deltaY'] < 0:
                        self.interactor.MouseWheelForwardEvent()
                    else:
                        self.interactor.MouseWheelBackwardEvent()
                    self.full_render()

        except Exception as e:
            self.error = str(e)

    def close(self):
        super().close()
        self._on_close()

    def __del__(self):
        super().__del__()
        self.close()
