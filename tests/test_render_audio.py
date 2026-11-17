from streamclipper.render import (
    COMP_STAGES,
    MAX_LIMITING_DB,
    aac_ceiling,
    comp_filter,
    limiting_db,
    next_stage,
    sfx_volume_cap,
    sustained_limiting,
)


def test_limiting_is_peak_above_the_limiter_ceiling():
    assert limiting_db(None) == 0.0
    assert limiting_db(-5.0) == 0.0
    assert abs(limiting_db(2.5) - 5.5) < 1e-9


def test_gain_staging_steps_up_only_when_limiting_exceeds_6_db():
    assert next_stage(0, MAX_LIMITING_DB) is None  # exactly 6 dB: fine
    assert next_stage(0, 7.0) == 1  # 7 dB of limiting -> compressor stage 1
    assert next_stage(1, 9.0) == 2
    assert next_stage(len(COMP_STAGES) - 1, 20.0) is None  # last stage: keep it
    assert comp_filter(0) == ""
    thr = []
    for k in range(1, len(COMP_STAGES)):
        comp = comp_filter(k)
        assert "ratio=" in comp and "attack=5" in comp and "release=120" in comp
        thr.append(float(comp.split("threshold=")[1].split(":")[0]))
    assert thr == sorted(thr, reverse=True)  # each stage compresses more


def test_sfx_stays_6_db_under_the_voice():
    # voice lane -20 LUFS, SFX file -10 LUFS: volume must give <= -26 LUFS -> <= 10^(-16/20)
    v = sfx_volume_cap(0.3, -20.0, -10.0)
    assert v <= 10 ** (-16 / 20) + 1e-4
    assert sfx_volume_cap(0.3, -10.0, -30.0) == 0.3  # quiet SFX keep their volume
    assert sfx_volume_cap(0.25, None, -10.0) == 0.25


def test_sustained_limiting_ignores_quiet_frames_and_transients():
    pre = [-60.0, -20.0, -8.0, -5.0, -12.0]
    post = [-60.0, -20.0, -9.0, -11.5, -12.0]
    assert sustained_limiting(pre, post) == 6.5
    assert sustained_limiting([-50.0], [-80.0]) == 0.0  # silence does not count
    assert sustained_limiting([], []) == 0.0


def test_aac_overshoot_lowers_the_limiter_ceiling():
    assert aac_ceiling(-3.0, -2.0) == -3.0
    assert aac_ceiling(-3.0, None) == -3.0
    assert aac_ceiling(-3.0, 0.2) == round(-3.0 - 1.4 - 0.3, 2)


def test_sustained_limiting_aligns_logs_that_differ_by_a_frame():
    pre = [-20.0] * 10 + [-6.0] * 10 + [-20.0] * 10
    post = [-120.0] + [-20.0] * 10 + [-9.0] * 10 + [-20.0] * 10 + [-150.0]  # one frame late, odd edges
    assert sustained_limiting(pre, post) == 3.0
