"""生成可重复演示的数据；--image 使用用户本地图片，不下载外部素材。"""

import shutil
from pathlib import Path

from core.paths import ensure_core_on_path, runtime_path


def prepare(image: str = "", *, destination=None) -> str:
    ensure_core_on_path()
    from fake_adapter import make_sandbox

    root = Path(destination) if destination is not None else Path(runtime_path("demo-sandbox", "integrated"))
    root.mkdir(parents=True, exist_ok=True)
    # 已有工单目录不覆盖，重复演示使用 --prepare-demo 之外的新副本或还原任务文件。
    if not (root / "calc.py").exists():
        make_sandbox(str(root))
    if image:
        target = root / "sample" / "bus.jpg"
        target.parent.mkdir(exist_ok=True)
        if Path(image).resolve() != target.resolve():
            shutil.copyfile(image, target)
    if not (root / "vision_classify.py").exists():
        (root / "vision_classify.py").write_text(
            '''from pathlib import Path


def classify_image(path: str) -> str:
    """本演示样本含公交车时返回 vehicle；不存在时返回 other。"""
    raise NotImplementedError
''',
            encoding="utf-8",
        )
        (root / "test_vision_classify.py").write_text(
            """import unittest
from vision_classify import classify_image


class TestVision(unittest.TestCase):
    def test_bus(self) -> None:
        self.assertEqual(classify_image("sample/bus.jpg"), "vehicle")

    def test_missing(self) -> None:
        self.assertEqual(classify_image("missing.jpg"), "other")


if __name__ == "__main__":
    unittest.main()
""",
            encoding="utf-8",
        )
    return str(root)
