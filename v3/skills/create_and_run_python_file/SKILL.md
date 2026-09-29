---
name: create_and_run_python_file
description: 在指定位置创建Python文件并运行。当用户要求创建并运行Python文件时使用。
---

## 何时使用

当用户需要在指定位置创建Python文件并运行该文件时使用此技能。

## 工作流步骤

### 1. 确定目标位置
- 使用 `list_dir` 查看当前目录结构
- 确认目标目录是否存在（如桌面、用户目录等）
- 如果路径不存在，使用相对路径或绝对路径

### 2. 创建Python文件
- 使用 `write_file` 在目标位置创建Python文件
- 文件名建议使用 `.py` 后缀
- 写入简单的Python代码（如 `print("Hello World")`）

### 3. 运行Python文件
- 使用 `run_shell` 切换到文件所在目录
- 执行 `python 文件名.py` 命令
- 检查运行结果和输出

### 4. 验证结果
- 确认程序成功运行
- 检查输出是否符合预期

## 示例

**用户需求**：在桌面创建并运行Python文件

**执行流程**：
1. `list_dir` 查看桌面位置
2. `write_file` 创建 `C:\Users\用户名\Desktop\hello.py`
3. `run_shell` 执行 `cd C:\Users\用户名\Desktop; python hello.py`
4. 验证输出 "Hello World"

## 边界情况

- 如果Python命令不存在，需要先安装Python
- 如果目标目录权限不足，需要调整权限或选择其他位置
- 如果文件名包含空格，需要在命令中使用引号
- 如果代码有语法错误，需要修正代码内容

## 工具依赖

- `list_dir`: 查看目录结构
- `write_file`: 创建和写入Python文件
- `run_shell`: 执行Python命令
- `read_file`: 可选，用于验证文件内容
