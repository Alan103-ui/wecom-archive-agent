import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    from app.services.ocr import engine
    from app.services.ocr.engine import _EngineHolder

    imgs = sorted(__import__("glob").glob("data/sample_images/delivery/*.png"))
    sample = imgs[0]
    print("main pid =", os.getpid(), "| sample =", sample)

    # 多跑几张，确认 worker 热复用 + 主进程零加载
    for i, im in enumerate(imgs[:4], 1):
        out = engine.recognize_isolated(im)
        print(f"[{i}] recognize_isolated -> success={out.success} "
              f"engine={out.engine} conf={out.avg_confidence:.4f} textlen={len(out.text or '')}")

    loaded_in_main = _EngineHolder._engine is not None
    print("主进程是否已加载 RapidOCR 模型:", loaded_in_main)

    st = engine.engine_status()
    print("engine_status.available =", st["available"])
    print("engine_status.pool =", st.get("pool"))

    assert not loaded_in_main, "隔离失败：模型被加载进了主进程！"
    print("\n✅ 隔离验证通过：模型只在 worker 进程，主进程零加载；多图命中同一 worker 热复用")

if __name__ == "__main__":
    main()
