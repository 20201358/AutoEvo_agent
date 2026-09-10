"""主入口"""
import sys
import argparse
from ui.cli import CLI
from ui.interactive import InteractiveCLI


def main():
    """主函数"""
    parser = argparse.ArgumentParser(description="终端命令 AI 助手")
    parser.add_argument(
        "--mode", 
        choices=["basic", "interactive"], 
        default="interactive",
        help="运行模式: basic(基础) 或 interactive(交互式)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="模拟执行命令 (默认启用)"
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="实际执行命令 (谨慎使用)"
    )
    
    args = parser.parse_args()
    
    # 如果没有参数，直接运行
    if len(sys.argv) == 1:
        cli = InteractiveCLI(dry_run=True)
        cli.run()
        return
    
    # 根据参数运行
    dry_run = not args.execute
    
    if args.mode == "basic":
        cli = CLI()
    else:
        cli = InteractiveCLI(dry_run=dry_run)
    
    cli.run()


if __name__ == "__main__":
    main()