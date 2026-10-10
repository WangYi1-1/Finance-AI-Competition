"""命令行入口，方便评审在无界面环境复现。"""
from __future__ import annotations

import argparse
from pathlib import Path

from .demo import ensure_demo_files
from .pipeline import run_analysis


def main() -> None:
    parser = argparse.ArgumentParser(description="财报异常信号识别原型")
    parser.add_argument("--demo", action="store_true", help="使用虚构财报，不调用DeepSeek")
    parser.add_argument("--files", nargs="+", type=Path, help="真实文字版财报PDF")
    parser.add_argument("--replay", type=Path, help="从保存的result.json离线重算，不调用API")
    parser.add_argument("--verify", type=Path, help="核对保存运行目录的artifacts.json，不调用API")
    parser.add_argument("--output", type=Path, default=Path("output"))
    args = parser.parse_args()
    selected = sum(bool(x) for x in (args.demo, args.files, args.replay, args.verify))
    if selected != 1:
        parser.error("请只选择--demo、--files、--replay或--verify中的一种。")
    if args.verify:
        from .provenance import verify_artifacts
        try:
            errors = verify_artifacts(args.verify)
        except (OSError, ValueError, KeyError):
            parser.exit(1, "核验失败：清单无法读取或结构不正确。\n")
        if errors:
            parser.exit(1, "核验失败：" + "；".join(errors) + "\n")
        print("全部成果文件与封存哈希一致。")
        return
    if args.replay:
        from .replay import replay_result
        try:
            result = replay_result(args.replay, args.output)
        except (OSError, ValueError, KeyError) as exc:
            parser.exit(1, f"回放未完成：{exc}\n")
        print(f"离线重算完成，未调用API：{result.run_dir}")
        return
    if not args.demo and not args.files:
        parser.error("请指定--demo或--files。")
    if args.demo and args.files:
        parser.error("虚构演示使用系统样例，请不要同时传--files。")
    try:
        result = run_analysis(ensure_demo_files() if args.demo else args.files,
                              mode="demo" if args.demo else "live", output_root=args.output, progress=print)
    except Exception as exc:
        if isinstance(exc, (ValueError, RuntimeError)):
            parser.exit(1, f"分析未完成：{exc}\n")
        parser.exit(1, "分析未完成，请检查本地依赖和输入文件。\n")
    print(f"完成：{len(result.reports)}份报告，{len(result.analysis.signals)}条评估结果。")
    print(f"报告位置：{result.run_dir}")


if __name__ == "__main__":
    main()
