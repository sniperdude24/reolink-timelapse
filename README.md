# reolink-timelapse

Pulls RTSP video from a Reolink IP camera (in parallel with your NVR's own recording) and grabs one frame every N seconds, so you can stitch them into a timelapse video later.

## Requirements

- Python 3.8+
- [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) on your PATH (`ffmpeg -version` should work)

## Usage

Grab a frame every 30 seconds:

```bash
python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass
```

Grab a frame every 5 seconds instead:

```bash
python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass --interval 5
```

Record, then build the timelapse video at 24fps:

```bash
python timelapse_recorder.py --ip 192.168.1.50 --user admin --password mypass --build --output-fps 24
```

Just build a video from frames you already grabbed (no recording):

```bash
python timelapse_recorder.py --build-only --output-fps 30 --frames-dir frames
```

## Notes

- `--password` is passed on the command line, which means it's visible in shell history and process listings (e.g. Task Manager's command-line column). Fine for personal/local use; avoid on a shared machine.
- Frames are named by timestamp (`%Y%m%d_%H%M%S.jpg`), so they sort chronologically for free.
