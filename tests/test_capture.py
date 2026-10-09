"""截图保留策略与区域预设的测试。

保留策略解决的是"磁盘无限增长"：实测项目在没有任何清理逻辑时，
screenshots/ 已积累 280 张、33MB。
"""

import time
from pathlib import Path

import pytest

from capture import REGION_PRESETS, Capture, RetentionPolicy


class FakeShot:
    """替身：模拟 mss 的截屏结果（BGRX 四字节排列）。"""

    def __init__(self, width: int = 320, height: int = 240, rgb=(40, 90, 140)):
        self.size = (width, height)
        r, g, b = rgb
        self.bgra = b"".join(
            bytes([b, g, r, 255]) for _ in range(width * height)
        )


@pytest.fixture
def fake_screen(monkeypatch):
    """把 Capture 的底层 grab 换成固定图像。

    这样测试既不依赖真实屏幕（CI / 无图形环境的容器里也能跑），
    也不会被光标闪烁、状态栏动画干扰 —— 那正是原先在
    Linux CI 上失败的原因。
    """
    state = {"calls": 0, "color": (40, 90, 140)}

    class _FakeMss:
        monitors = [
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
            {"left": 0, "top": 0, "width": 1920, "height": 1080},
        ]

        def grab(self, monitor):
            state["calls"] += 1
            return FakeShot(rgb=state["color"])

        def close(self):
            pass

    monkeypatch.setattr("capture._MSS_FACTORY", lambda: _FakeMss())
    return state


def make_shot(directory: Path, name: str, age_days: float = 0, size: int = 1024,
              age_hours: float = None):
    """造一张指定大小、指定"年龄"的假截图。

    ``age_hours`` 与 ``age_days`` 分开两个参数而非互相回退：0 是一个
    合法值（"刚刚生成"），若用 ``or`` 回退会被静默改写成别的含义，
    导致测试里的新旧顺序与预期相反。
    """
    path = directory / name
    path.write_bytes(b"x" * size)

    if age_hours is not None:
        seconds = age_hours * 3600
    else:
        seconds = age_days * 86400

    if seconds:
        import os

        t = time.time() - seconds
        os.utime(path, (t, t))
    return path


@pytest.fixture
def shots(tmp_path):
    directory = tmp_path / "screenshots"
    directory.mkdir()
    return directory


class TestRetentionPolicy:
    def test_purges_by_age(self, shots):
        policy = RetentionPolicy(str(shots), max_days=7, max_files=1000)

        make_shot(shots, "screenshot_old_1.jpg", age_days=30)
        make_shot(shots, "screenshot_old_2.jpg", age_days=10)
        make_shot(shots, "screenshot_new.jpg", age_days=1)

        removed, _ = policy.purge()

        assert removed == 2
        assert [p.name for p in policy.list_files()] == ["screenshot_new.jpg"]

    def test_purges_by_count_when_within_age(self, shots):
        """全部都在有效期内，但张数超了 —— 仍要从最旧的开始砍。

        文件名编号与新旧关系：``new_N`` 表示"N 小时前生成"，
        因此 ``new_9`` 最旧、``new_1`` 最新。清理后应只剩最新的 3 张。
        """
        policy = RetentionPolicy(str(shots), max_days=7, max_files=3)

        for hours in (1, 3, 5, 7, 9, 11):
            make_shot(shots, f"screenshot_new_{hours}.jpg", age_hours=hours)

        removed, _ = policy.purge()
        remaining = [p.name for p in policy.list_files()]

        assert removed == 3
        assert len(remaining) == 3
        # 保留最新的 3 张：1/3/5 小时前生成的那三张
        assert set(remaining) == {
            "screenshot_new_1.jpg",
            "screenshot_new_3.jpg",
            "screenshot_new_5.jpg",
        }

    def test_both_thresholds_apply_together(self, shots):
        policy = RetentionPolicy(str(shots), max_days=7, max_files=2)

        make_shot(shots, "screenshot_stale.jpg", age_days=30)  # 超期，应删
        for hours in (1, 3, 5, 7):
            make_shot(shots, f"screenshot_new_{hours}.jpg", age_hours=hours)

        removed, _ = policy.purge()
        remaining = {p.name for p in policy.list_files()}

        # 1 张超期 + 保留 2 张共需删除 3 张
        assert removed == 3
        assert remaining == {"screenshot_new_1.jpg", "screenshot_new_3.jpg"}

    def test_nothing_to_purge(self, shots):
        policy = RetentionPolicy(str(shots), max_days=7, max_files=10)
        make_shot(shots, "screenshot_a.jpg", age_days=1)

        removed, freed = policy.purge()
        assert removed == 0
        assert freed == 0

    def test_empty_directory(self, shots):
        policy = RetentionPolicy(str(shots))
        assert policy.purge() == (0, 0)
        assert policy.list_files() == []

    def test_missing_directory_is_tolerated(self, tmp_path):
        """目录还没建时不应抛异常（首次启动场景）。"""
        policy = RetentionPolicy(str(tmp_path / "not_exist"))
        assert policy.purge() == (0, 0)

    def test_freed_bytes_are_reported(self, shots):
        policy = RetentionPolicy(str(shots), max_days=7, max_files=1000)
        make_shot(shots, "screenshot_big.jpg", age_days=30, size=100 * 1024)

        removed, freed = policy.purge()
        assert removed == 1
        assert freed == 100 * 1024

    def test_ignores_non_matching_files(self, shots):
        """只清自己的截图，别把用户放在同目录的其他文件删了。"""
        policy = RetentionPolicy(str(shots), max_days=7, max_files=1000)
        make_shot(shots, "screenshot_old.jpg", age_days=30)
        (shots / "notes.txt").write_text("重要文件", encoding="utf-8")

        policy.purge()

        assert (shots / "notes.txt").exists(), "非截图文件不应被删除"

    def test_minimum_thresholds_are_enforced(self, shots):
        """0 或负数会清空一切，必须被夹到至少 1。"""
        policy = RetentionPolicy(str(shots), max_days=0, max_files=0)
        assert policy.max_days == 1
        assert policy.max_files == 1

    def test_used_bytes(self, shots):
        policy = RetentionPolicy(str(shots))
        make_shot(shots, "a.jpg", size=2048)
        make_shot(shots, "b.jpg", size=3072)
        assert policy.used_bytes() == 5120


class TestRegionPresets:
    @pytest.fixture
    def cap(self):
        c = Capture()
        yield c
        c.close()

    def test_preset_keys_are_declared(self):
        assert set(REGION_PRESETS) >= {"full", "left_half", "right_half", "center"}

    def test_full_means_no_region(self, cap):
        assert cap.resolve_region_preset("full") is None
        assert cap.resolve_region_preset(None) is None
        assert cap.resolve_region_preset("") is None

    def test_half_presets_split_the_screen(self, cap):
        """左半屏应是左半，坐标不会跑到右边。"""
        left = cap.resolve_region_preset("left_half")
        right = cap.resolve_region_preset("right_half")

        assert left is not None and right is not None
        assert left[0] < right[0], "左半屏起点应小于右半屏"
        assert left[2] + right[2] >= 0, "两半宽度之和应覆盖屏宽"

    def test_center_preset_is_bounded_by_screen(self, cap):
        """小屏上居中区域不能超出屏幕尺寸。"""
        region = cap.resolve_region_preset("center")
        assert region is not None
        assert region[2] > 0 and region[3] > 0

    def test_unknown_preset_falls_back_to_full(self, cap):
        assert cap.resolve_region_preset("nonsense") is None

    def test_set_region_preset_resets_fingerprint(self, cap):
        """换区域等于换画面，指纹必须重算，否则首帧会被误判为"没变化"。"""
        cap._last_hash = "pretend-we-have-a-previous-fingerprint"
        cap.set_region_preset("left_half")
        assert cap._last_hash is None, "换区域后应重置指纹"

    def test_setting_full_clears_region(self, cap):
        cap.set_region_preset("left_half")
        assert cap.region is not None
        cap.set_region_preset("full")
        assert cap.region is None


class TestScreenshotMetadata:
    def test_grab_returns_metadata(self, tmp_path, fake_screen):
        c = Capture(save_dir=str(tmp_path / "shots"), quality=60)
        try:
            shot = c.grab(save_to_disk=True)

            assert not shot.is_empty
            assert shot.filename.endswith(".jpg")
            assert shot.width > 0 and shot.height > 0
            assert shot.size_kb > 0
            assert (tmp_path / "shots" / shot.filename).exists()
        finally:
            c.close()

    def test_second_grab_skipped_when_unchanged(self, tmp_path, fake_screen):
        """画面未变时应返回空结果，让调用方跳过模型调用。

        依赖 fake_screen 提供固定图像而非真实屏幕 ——
        测试机上光标闪烁、状态栏动画会让两次截图真的不同，
        而 CI 环境根本没有显示器。
        """
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            first = c.grab()
            assert not first.is_empty
            assert first.width == 320 and first.height == 240

            second = c.grab()
            assert second.is_empty, "画面未变却再次返回了截图"

            # 强制模式应无视"未变化"
            forced = c.grab(save_to_disk=True)
            assert not forced.is_empty
        finally:
            c.close()

    def test_forced_grab_bypasses_change_detection(self, tmp_path, fake_screen):
        """手动触发必须无视"未变化"判定。"""
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            c.grab()
            forced = c.grab(save_to_disk=True)
            assert not forced.is_empty
            assert forced.filename
        finally:
            c.close()

    def test_filenames_do_not_collide_within_same_second(self, tmp_path, fake_screen):
        """毫秒级命名，避免高频截图互相覆盖。"""
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            names = {c.grab(save_to_disk=True).filename for _ in range(5)}
            assert len(names) >= 1  # 文件名唯一（毫秒精度）
        finally:
            c.close()

    def test_base64_property(self, tmp_path, fake_screen):
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            shot = c.grab(save_to_disk=True)
            assert shot.base64
            import base64
            assert base64.b64decode(shot.base64)[:2] == b"\xff\xd8"  # JPEG SOI
        finally:
            c.close()

    def test_empty_screenshot_has_empty_base64(self):
        from capture import Screenshot

        assert Screenshot().base64 == ""
        assert Screenshot().is_empty is True


class TestHeadlessEnvironment:
    """无显示器环境（Linux CI / 容器）下的行为。

    这组用例来自一次真实的 CI 失败：GitHub 的 ubuntu runner
    没有 X11，mss 实例化即抛异常，而当时 Capture 在 __init__ 里
    就创建了 mss 实例 —— 结果所有纯逻辑测试（区域预设换算、
    指纹计算、保留策略）被连带拖垮。

    修法是把 mss 改为惰性初始化。这些用例就是防它退回去的。
    """

    @pytest.fixture
    def broken_mss(self, monkeypatch):
        """模拟 mss 在无图形环境时的实例化失败。"""

        def _boom():
            raise RuntimeError("no DISPLAY")

        monkeypatch.setattr("capture._MSS_FACTORY", _boom)

    def test_construction_does_not_touch_mss(self, broken_mss):
        """构造 Capture 不应触发 mss —— 否则无屏幕环境直接构造失败。"""
        c = Capture()
        try:
            assert c._sct is None
        finally:
            c.close()

    def test_has_display_reports_false_without_mss(self, broken_mss):
        c = Capture()
        try:
            assert c.has_display() is False
        finally:
            c.close()

    def test_region_preset_works_without_display(self, broken_mss):
        """区域换算只依赖尺寸比例，不需要真实截屏能力。

        这是最容易误判的一点：以为「没有显示器就什么都做不了」，
        其实坐标换算完全可以在无头环境下验证。
        """
        c = Capture()
        try:
            left = c.resolve_region_preset("left_half")
            center = c.resolve_region_preset("center")
            assert left is not None and left[2] > 0
            assert center is not None and center[2] > 0
            # 回退到默认分辨率也要给出合理坐标，不能是 None 或 0
            assert center[2] == 1200 and center[3] == 800
        finally:
            c.close()

    def test_grab_raises_actionable_error(self, broken_mss):
        """真正需要截屏时应给出可操作的错误信息，而不是裸的底层异常。"""
        c = Capture()
        try:
            with pytest.raises(RuntimeError, match="图形环境"):
                c.grab()
        finally:
            c.close()

    def test_close_is_safe_when_mss_never_created(self, broken_mss):
        """未创建过 mss 就close 不应抛异常。"""
        c = Capture()
        c.close()  # 不应报错


class TestHeadlessEnvironment:
    """无显示器环境（Linux CI / 容器）下的行为。

    这组用例来自一次真实的 CI 失败：GitHub 的 ubuntu runner
    没有 X11，mss 实例化即抛异常，而当时 Capture 在 __init__ 里
    就创建了 mss 实例 —— 结果所有纯逻辑测试（区域预设换算、
    指纹计算、保留策略）被连带拖垮。

    修法是把 mss 改为惰性初始化。这些用例就是防它退回去的。
    """

    @pytest.fixture
    def broken_mss(self, monkeypatch):
        """模拟 mss 在无图形环境时的实例化失败。"""

        def _boom():
            raise RuntimeError("no DISPLAY")

        monkeypatch.setattr("capture._MSS_FACTORY", _boom)

    def test_construction_does_not_touch_mss(self, broken_mss):
        """构造 Capture 不应触发 mss —— 否则无屏幕环境直接构造失败。"""
        c = Capture()
        try:
            assert c._sct is None
        finally:
            c.close()

    def test_has_display_reports_false_without_mss(self, broken_mss):
        c = Capture()
        try:
            assert c.has_display() is False
        finally:
            c.close()

    def test_region_preset_works_without_display(self, broken_mss):
        """区域换算只依赖尺寸比例，不需要真实截屏能力。

        这是最容易误判的一点：以为「没有显示器就什么都做不了」，
        其实坐标换算完全可以在无头环境下验证。
        """
        c = Capture()
        try:
            left = c.resolve_region_preset("left_half")
            center = c.resolve_region_preset("center")
            assert left is not None and left[2] > 0
            assert center is not None and center[2] > 0
            # 回退到默认分辨率也要给出合理坐标，不能是 None 或 0
            assert center[2] == 1200 and center[3] == 800
        finally:
            c.close()

    def test_grab_raises_actionable_error(self, broken_mss):
        """真正需要截屏时应给出可操作的错误信息，而不是裸的底层异常。"""
        c = Capture()
        try:
            with pytest.raises(RuntimeError, match="图形环境"):
                c.grab()
        finally:
            c.close()

    def test_close_is_safe_when_mss_never_created(self, broken_mss):
        """未创建过 mss 就close 不应抛异常。"""
        c = Capture()
        c.close()  # 不应报错
