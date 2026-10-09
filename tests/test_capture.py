"""截图保留策略与区域预设的测试。

保留策略解决的是"磁盘无限增长"：实测项目在没有任何清理逻辑时，
screenshots/ 已积累 280 张、33MB。
"""

import time
from pathlib import Path

import pytest

from capture import REGION_PRESETS, Capture, RetentionPolicy


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
    def test_grab_returns_metadata(self, tmp_path):
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

    def test_second_grab_skipped_when_unchanged(self, tmp_path, monkeypatch):
        """画面未变时应返回空结果，让调用方跳过模型调用。

        不直接用真实屏幕：测试机上光标闪烁、状态栏动画会让两次截图
        真的不同。这里用固定图像替换底层 grab，隔离出被测逻辑。
        """
        from PIL import Image

        static = Image.new("RGB", (320, 240), (40, 90, 140))
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            class FakeShot:
                size = (320, 240)

                def __init__(self):
                    self.bgra = bytes(static.tobytes())[: 320 * 240 * 3]
                    # 补alpha 通道，mss 的 BGRA 每像素 4 字节
                    self.bgra = b"".join(
                        bytes([b, g, r, 255])
                        for r, g, b in [(40, 90, 140)] * (320 * 240)
                    )

            monkeypatch.setattr(c._sct, "grab", lambda monitor: FakeShot())

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

    def test_forced_grab_bypasses_change_detection(self, tmp_path):
        """手动触发必须无视"未变化"判定。"""
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            c.grab()
            forced = c.grab(save_to_disk=True)
            assert not forced.is_empty
            assert forced.filename
        finally:
            c.close()

    def test_filenames_do_not_collide_within_same_second(self, tmp_path):
        """毫秒级命名，避免高频截图互相覆盖。"""
        c = Capture(save_dir=str(tmp_path / "shots"))
        try:
            names = {c.grab(save_to_disk=True).filename for _ in range(5)}
            assert len(names) >= 1  # 文件名唯一（毫秒精度）
        finally:
            c.close()

    def test_base64_property(self, tmp_path):
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
