"""Runtime identity for binding feature checks to the active llama settings."""
import hashlib
import json


def runtime_identity(props, expected_model):
    if not isinstance(props, dict) or props.get('model_path') != expected_model:
        raise ValueError('Loaded model does not match')
    template, build = props.get('chat_template'), props.get('build_info')
    settings, caps, modalities = (props.get(k) for k in ('default_generation_settings', 'chat_template_caps', 'modalities'))
    if (not isinstance(template, str) or not template or not isinstance(build, str) or not build
            or len(build) > 256 or not isinstance(settings, dict) or not isinstance(caps, dict)
            or not isinstance(modalities, dict)):
        raise ValueError('Runtime identity unavailable')
    encoded = json.dumps({'default_generation_settings': settings, 'chat_template_caps': caps, 'modalities': modalities},
                         sort_keys=True, ensure_ascii=True, separators=(',', ':'), allow_nan=False).encode('ascii')
    return {'build_info': build, 'chat_template_sha256': hashlib.sha256(template.encode('utf-8')).hexdigest(),
            'runtime_settings_sha256': hashlib.sha256(encoded).hexdigest()}
