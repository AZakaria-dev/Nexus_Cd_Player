"""Local UI service: a thread-safe HTTP/JSON adapter around CDController.

    QML (qmlscene)
        |  localhost HTTP + JSON, 127.0.0.1:8765
        v
    ui_service.server        <- HTTP threads, never touch CDController
        |  command queue + cached snapshot
        v
    ui_service.worker        <- THE ONLY thread that owns CDController
        v
    CDController -> PlaybackEngine -> icedax -> aplay

Exists because controller.play() blocks 5-8 s while the drive spins up, which
a touchscreen cannot afford to wait on. Nothing in backend/ is modified.

Python 3.5.2 compatible.
"""
