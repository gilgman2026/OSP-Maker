import json
import math
import os
import random
import string
import subprocess

VIDEO_LAYER = 1000000
SVG_LAYER = 2000000


class FFprobeError(Exception):
    pass


def _find_ffprobe():
    try:
        subprocess.run(
            ["ffprobe", "-version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True
        )
        return "ffprobe"
    except (FileNotFoundError, subprocess.CalledProcessError):
        return None


def _probe_video(path):
    """Return dict of real video metadata via ffprobe. Raises FFprobeError on failure."""
    ffprobe = _find_ffprobe()
    if not ffprobe:
        raise FFprobeError(
            "ffprobe was not found on PATH. Install ffmpeg and add its bin folder to PATH, "
            "then restart the application."
        )
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "quiet",
                "-print_format", "json",
                "-show_format",
                "-show_streams",
                path
            ],
            capture_output=True, text=True, check=True
        )
        data = json.loads(result.stdout)
    except subprocess.CalledProcessError as e:
        raise FFprobeError(f"ffprobe failed reading {path}: {e.stderr or e}") from e
    except json.JSONDecodeError as e:
        raise FFprobeError(f"ffprobe output was not valid JSON: {e}") from e

    info = {}
    fmt = data.get("format", {})
    info["duration"] = float(fmt.get("duration", 3600))
    info["file_size"] = int(fmt.get("size", 0))
    info["metadata"] = fmt.get("tags", {})

    for stream in data.get("streams", []):
        codec_type = stream.get("codec_type", "")
        if codec_type == "video" and "width" not in info:
            info["width"] = stream.get("width", 2560)
            info["height"] = stream.get("height", 1440)
            info["vcodec"] = stream.get("codec_name", "h264")
            info["video_bit_rate"] = int(stream.get("bit_rate", 0))
            info["video_stream_index"] = stream.get("index", 0)

            rfps = stream.get("r_frame_rate", "60/1")
            num, den = (int(x) for x in rfps.split("/"))
            info["fps"] = {"num": num, "den": den}
            fps_val = num / den
            info["fps_value"] = fps_val

            tb = stream.get("time_base", "1/60")
            tb_num, tb_den = (int(x) for x in tb.split("/"))
            info["video_timebase"] = {"den": num, "num": tb_num}

            nb_frames = stream.get("nb_frames")
            info["video_length"] = int(nb_frames) if nb_frames else int(math.ceil(info["duration"] * fps_val))

            dar = stream.get("display_aspect_ratio", "")
            if ":" in dar:
                dr_num, dr_den = (int(x) for x in dar.split(":"))
                info["display_ratio"] = {"num": dr_num, "den": dr_den}
            else:
                from math import gcd
                g = gcd(info["width"], info["height"])
                info["display_ratio"] = {"num": info["width"] // g, "den": info["height"] // g}

        elif codec_type == "audio" and "acodec" not in info:
            info["acodec"] = stream.get("codec_name", "aac")
            info["sample_rate"] = int(stream.get("sample_rate", 48000))
            info["channels"] = stream.get("channels", 2)
            info["audio_bit_rate"] = int(stream.get("bit_rate", 128000))
            info["audio_stream_index"] = stream.get("index", 1)
            ch_layout = stream.get("channel_layout", "stereo")
            info["channel_layout"] = 3 if ch_layout in ("stereo", "downmix") else 4

    return info


def _random_id(length=10):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def _kf(value):
    """OpenShot constant keyframe — uniform handle format used by all keyframes."""
    return {
        "Points": [
            {
                "co": {"X": 1.0, "Y": value},
                "handle_left":  {"X": 0.5, "Y": 1.0},
                "handle_right": {"X": 0.5, "Y": 0.0},
                "handle_type":  0,
                "interpolation": 0
            }
        ]
    }


def _wave_color():
    return {
        "alpha": _kf(255.0),
        "blue":  _kf(255.0),
        "green": _kf(123.0),
        "red":   _kf(0.0)
    }


_STANDARD_PROFILES = [
    (7680, 4320, "8K UHD 4320p"),
    (3840, 2160, "4K UHD 2160p"),
    (2560, 1440, "2.5K WQHD 1440p"),
    (1920, 1080, "HD 1080p"),
    (1280,  720, "HD 720p"),
    ( 854,  480, "SD 480p"),
]


def _nearest_profile(width, height):
    """Return the largest standard profile dims that fit within the video dimensions."""
    for pw, ph, _label in _STANDARD_PROFILES:
        if pw <= width and ph <= height:
            return pw, ph
    return width, height


def _profile_name(width, height, fps_num, fps_den):
    fps_int = int(round(fps_num / fps_den)) if fps_den else 30
    for pw, ph, label in _STANDARD_PROFILES:
        if pw == width and ph == height:
            return f"{label} {fps_int} fps"
    return f"{width}x{height} {fps_int} fps"


def _normalize_fps(fps_num, fps_den):
    """Round a fractional fps (e.g. 60000/1001) to the nearest integer for the project level."""
    fps = fps_num / fps_den if fps_den else 30
    fps_int = int(round(fps))
    return {"num": fps_int, "den": 1}


# ── File list entries (flat — shown in OpenShot's media library) ───────────

def _video_file_entry(path, file_id, probe=None):
    p = probe or {}
    return {
        "acodec":             p.get("acodec", "aac"),
        "audio_bit_rate":     p.get("audio_bit_rate", 128000),
        "audio_stream_index": p.get("audio_stream_index", 1),
        "audio_timebase":     {"den": p.get("sample_rate", 48000), "num": 1},
        "channel_layout":     p.get("channel_layout", 3),
        "channels":           p.get("channels", 2),
        "display_ratio":      p.get("display_ratio", {"den": 9, "num": 16}),
        "duration":           p.get("duration", 3600.0),
        "file_size":          str(p.get("file_size", 0)),
        "fps":                p.get("fps", {"den": 1, "num": 60}),
        "has_audio":          True,
        "has_single_image":   False,
        "has_video":          True,
        "height":             p.get("height", 1440),
        "interlaced_frame":   False,
        "metadata":           p.get("metadata", {}),
        "path":               path.replace("\\", "/"),
        "pixel_format":       12,
        "pixel_ratio":        {"den": 1, "num": 1},
        "sample_rate":        p.get("sample_rate", 48000),
        "top_field_first":    True,
        "type":               "FFmpegReader",
        "vcodec":             p.get("vcodec", "h264"),
        "video_bit_rate":     p.get("video_bit_rate", 0),
        "video_length":       str(p.get("video_length", 108000)),
        "video_stream_index": p.get("video_stream_index", 0),
        "video_timebase":     p.get("video_timebase", {"den": 60, "num": 1}),
        "width":              p.get("width", 2560),
        "media_type":         "video",
        "id":                 file_id,
        "image":              ""
    }


def _svg_file_entry(path, file_id):
    return {
        "acodec":             "",
        "audio_bit_rate":     0,
        "audio_stream_index": -1,
        "audio_timebase":     {"den": 1, "num": 1},
        "channel_layout":     4,
        "channels":           0,
        "display_ratio":      {"den": 9, "num": 16},
        "duration":           3600.0,
        "file_size":          "8294400",
        "fps":                {"den": 1, "num": 30},
        "has_audio":          False,
        "has_single_image":   True,
        "has_video":          True,
        "height":             1080,
        "interlaced_frame":   False,
        "metadata":           {},
        "path":               path.replace("\\", "/"),
        "pixel_format":       -1,
        "pixel_ratio":        {"den": 1, "num": 1},
        "sample_rate":        0,
        "top_field_first":    True,
        "type":               "QtImageReader",
        "vcodec":             "",
        "video_bit_rate":     0,
        "video_length":       "108000",
        "video_stream_index": -1,
        "video_timebase":     {"den": 30, "num": 1},
        "width":              1920,
        "media_type":         "image",
        "id":                 file_id,
        "image":              ""
    }


# ── Clip readers (embedded inside each timeline clip) ──────────────────────

def _video_clip_reader(path, file_id, probe=None):
    p = probe or {}
    return {
        "acodec":             p.get("acodec", "aac"),
        "audio_bit_rate":     p.get("audio_bit_rate", 128000),
        "audio_stream_index": p.get("audio_stream_index", 1),
        "audio_timebase":     {"den": p.get("sample_rate", 48000), "num": 1},
        "channel_layout":     p.get("channel_layout", 3),
        "channels":           p.get("channels", 2),
        "display_ratio":      p.get("display_ratio", {"den": 9, "num": 16}),
        "duration":           p.get("duration", 3600.0),
        "file_size":          p.get("file_size", 0),
        "fps":                p.get("fps", {"den": 1, "num": 60}),
        "has_audio":          True,
        "has_single_image":   False,
        "has_video":          True,
        "height":             p.get("height", 1440),
        "id":                 file_id,
        "image":              "",
        "interlaced_frame":   False,
        "media_type":         "video",
        "metadata":           p.get("metadata", {}),
        "path":               path.replace("\\", "/"),
        "pixel_format":       12,
        "pixel_ratio":        {"den": 1, "num": 1},
        "sample_rate":        p.get("sample_rate", 48000),
        "top_field_first":    True,
        "type":               "FFmpegReader",
        "vcodec":             p.get("vcodec", "h264"),
        "video_bit_rate":     p.get("video_bit_rate", 0),
        "video_length":       p.get("video_length", 108000),
        "video_stream_index": p.get("video_stream_index", 0),
        "video_timebase":     p.get("video_timebase", {"den": 60, "num": 1}),
        "width":              p.get("width", 2560)
    }


def _svg_clip_reader(path, file_id):
    return {
        "acodec":             "",
        "audio_bit_rate":     0,
        "audio_stream_index": -1,
        "audio_timebase":     {"den": 1, "num": 1},
        "channel_layout":     4,
        "channels":           0,
        "display_ratio":      {"den": 9, "num": 16},
        "duration":           3600.0,
        "file_size":          8294400,
        "fps":                {"den": 1, "num": 30},
        "has_audio":          False,
        "has_single_image":   True,
        "has_video":          True,
        "height":             1080,
        "id":                 file_id,
        "image":              "",
        "interlaced_frame":   False,
        "media_type":         "image",
        "metadata":           {},
        "path":               path.replace("\\", "/"),
        "pixel_format":       -1,
        "pixel_ratio":        {"den": 1, "num": 1},
        "sample_rate":        0,
        "top_field_first":    True,
        "type":               "QtImageReader",
        "vcodec":             "",
        "video_bit_rate":     0,
        "video_length":       108000,
        "video_stream_index": -1,
        "video_timebase":     {"den": 30, "num": 1},
        "width":              1920
    }


def _make_clip(position, start, end, layer, reader, title, file_id,
               loc_x=0.0, loc_y=0.0, scale_x=1.0, scale_y=1.0, rotation=0.0):
    return {
        "alpha":            _kf(1.0),
        "anchor":           0,
        "channel_filter":   _kf(-1.0),
        "channel_mapping":  _kf(-1.0),
        "display":          0,
        "duration":         round(end - start, 6),
        "effects":          [],
        "end":              end,
        "file_id":          file_id,
        "gravity":          4,
        "has_audio":        _kf(-1.0),
        "has_video":        _kf(-1.0),
        "id":               _random_id(),
        "image":            "",
        "layer":            layer,
        "location_x":       _kf(loc_x),
        "location_y":       _kf(loc_y),
        "mixing":           0,
        "origin_x":         _kf(0.5),
        "origin_y":         _kf(0.5),
        "parentObjectId":   "",
        "perspective_c1_x": _kf(-1.0),
        "perspective_c1_y": _kf(-1.0),
        "perspective_c2_x": _kf(-1.0),
        "perspective_c2_y": _kf(-1.0),
        "perspective_c3_x": _kf(-1.0),
        "perspective_c3_y": _kf(-1.0),
        "perspective_c4_x": _kf(-1.0),
        "perspective_c4_y": _kf(-1.0),
        "position":         round(position, 6),
        "reader":           reader,
        "rotation":         _kf(rotation),
        "scale":            1,
        "scale_x":          _kf(scale_x),
        "scale_y":          _kf(scale_y),
        "shear_x":          _kf(0.0),
        "shear_y":          _kf(0.0),
        "start":            round(start, 6),
        "time":             _kf(1.0),
        "title":            title,
        "volume":           _kf(1.0),
        "wave_color":       _wave_color(),
        "waveform":         False
    }


def generate_osp(video_path, svg_dir, clips, output_path,
                 loc_x=0.0, loc_y=0.0, scale_x=1.0, scale_y=1.0, rotation=0.0):
    """
    Generate an OpenShot project (.osp) file.

    clips: list of dicts with keys:
      - start (float): start time in the source video, in seconds
      - end   (float): end time in the source video, in seconds
      - svg_file (str): filename of the SVG overlay (relative to svg_dir)
    """
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    probe = _probe_video(video_path)  # raises FFprobeError on failure
    p = probe
    video_duration = float(p.get("duration", 3600.0))

    video_title = os.path.basename(video_path)
    video_file_id = _random_id()

    files_map = {}
    files_map[os.path.normpath(video_path)] = _video_file_entry(video_path, video_file_id, probe)

    svg_id_map = {}
    osp_clips = []
    timeline_position = 0.0

    for clip_data in clips:
        clip_start  = float(clip_data["start"])
        clip_end    = float(clip_data["end"])
        clip_dur    = clip_end - clip_start
        svg_file    = clip_data["svg_file"]
        svg_path    = os.path.join(svg_dir, svg_file)
        svg_norm    = os.path.normpath(svg_path)

        if svg_norm not in files_map:
            svg_fid = _random_id()
            files_map[svg_norm] = _svg_file_entry(svg_path, svg_fid)
            svg_id_map[svg_norm] = svg_fid
        svg_file_id = svg_id_map.get(svg_norm, files_map[svg_norm]["id"])

        osp_clips.append(_make_clip(
            position=timeline_position,
            start=clip_start,
            end=clip_end,
            layer=VIDEO_LAYER,
            reader=_video_clip_reader(video_path, video_file_id, probe),
            title=video_title,
            file_id=video_file_id,
            loc_x=loc_x,
            loc_y=loc_y,
            scale_x=scale_x,
            scale_y=scale_y,
            rotation=rotation,
        ))

        osp_clips.append(_make_clip(
            position=timeline_position,
            start=0.0,
            end=clip_dur,
            layer=SVG_LAYER,
            reader=_svg_clip_reader(svg_path, svg_file_id),
            title=svg_file,
            file_id=svg_file_id,
        ))

        timeline_position += clip_dur

    raw_fps  = p.get("fps", {"num": 60, "den": 1})
    proj_fps = _normalize_fps(raw_fps["num"], raw_fps["den"])
    vid_w    = p.get("width", 2560)
    vid_h    = p.get("height", 1440)
    width, height = _nearest_profile(vid_w, vid_h)
    duration = round(timeline_position, 6) if timeline_position > 0 else 0.0

    project = {
        "id":                _random_id(),
        "fps":               proj_fps,
        "display_ratio":     {"num": 16, "den": 9},
        "pixel_ratio":       {"num": 1, "den": 1},
        "width":             width,
        "height":            height,
        "sample_rate":       p.get("sample_rate", 48000),
        "channels":          p.get("channels", 2),
        "channel_layout":    p.get("channel_layout", 3),
        "settings":          {},
        "clips":             osp_clips,
        "effects":           [],
        "files":             list(files_map.values()),
        "layers": [
            {"id": "L1", "label": "", "number": 1000000, "y": 0, "lock": False},
            {"id": "L2", "label": "", "number": 2000000, "y": 0, "lock": False},
            {"id": "L3", "label": "", "number": 3000000, "y": 0, "lock": False},
            {"id": "L4", "label": "", "number": 4000000, "y": 0, "lock": False},
            {"id": "L5", "label": "", "number": 5000000, "y": 0, "lock": False},
        ],
        "markers":           [],
        "playhead_position": 0,
        "profile":           _profile_name(width, height, proj_fps["num"], proj_fps["den"]),
        "progress":          [],
        "scale":             round(duration / 20.0, 6) if duration > 0 else 15,
        "tick_pixels":       100,
        "version": {
            "openshot-qt":   "3.3.0",
            "libopenshot":   "0.4.0"
        },
        "duration":          duration,
        "export_settings":   None,
    }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(project, f, indent=1)
