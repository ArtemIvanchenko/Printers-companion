import json
import locale
import sys

import pytest

from core.updating.runtime import Commands


@pytest.mark.parametrize('encoding', ['cp1251', 'cp1252'])
def test_host_command_preserves_utf8_paths_under_windows_locale(tmp_path, monkeypatch, encoding):
    # Exercise real process pipes under a legacy host text encoding. The child
    # emits the UTF-8 JSON used by Docker, without starting Docker or a worker.
    monkeypatch.setattr(locale, 'getencoding', lambda: encoding)
    expected = {'Source': 'C:/Печати/оператор', 'Name': 'деталь'}
    payload = json.dumps(expected, ensure_ascii=False).encode('utf-8')
    result = Commands(tmp_path).run([
        sys.executable, '-c', f'import sys; sys.stdout.buffer.write({payload!r})',
    ])
    assert json.loads(result) == expected
