import pytest

import rafagas

LOG = """\
[libdrc] frames=43 idr=8 resync=0 kbps=108 enc_avg=7.1ms
[libdrc] resync tick=100 since_idr_tick=50 pending=0 last=1,2,3
[libdrc] resync tick=101 since_idr_tick=51 pending=1 last=1,2,3
[libdrc] resync tick=102 since_idr_tick=0 pending=0 last=-9,1,2
[libdrc] resync tick=110 since_idr_tick=8 pending=0 last=1,2,3 ignored=1
[libdrc] frames=40 idr=1 resync=4 kbps=100 enc_avg=7.0ms
[libdrc] resync tick=300 since_idr_tick=200 pending=0 last=1,2,3
[libdrc] resync tick=301 since_idr_tick=201 pending=1 last=1,2,3
"""


def test_rafagas_cuenta_rafagas_idr_y_tiempo_en_resync():
    """
    Prueba 111: con un log sintético, una ráfaga termina tras 20 ticks sin resync. Dos ráfagas (ticks 100-110 y
    300-301); IDR distintos por ráfaga = valores distintos de tick - since_idr_tick (50 y 102 en la primera, 100 en la
    segunda: media 1,5); tiempo en resync = ticks cubiertos por las ráfagas (11 + 2) sobre el total (6000 ticks).
    """
    r = rafagas.analyze(LOG.splitlines(), total_ticks=6000)
    assert r["rafagas"] == 2
    assert r["rafagas_por_min"] == pytest.approx(2 / (6000 / 59.94 / 60))
    assert r["idr_por_rafaga"] == pytest.approx(1.5)
    assert r["pct_resync"] == pytest.approx(13 / 6000 * 100)


def test_rafagas_sin_resync_devuelve_ceros():
    """Prueba 112: un log sin líneas de resync (o vacío) devuelve ceros, sin error."""
    for lines in ([], ["[libdrc] frames=43 idr=0 resync=0 kbps=5", "otra cosa"]):
        r = rafagas.analyze(lines, total_ticks=6000)
        assert r == {"rafagas": 0, "rafagas_por_min": 0.0, "idr_por_rafaga": 0.0, "pct_resync": 0.0}
