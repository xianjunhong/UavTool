# UavTool 环境安装教程

本文档用于在一台新电脑上安装 UavTool 的运行环境。

推荐使用 Conda 安装环境。不要直接在普通 `venv` 里执行 `pip install -r requirements.txt`，因为 Windows 下 `GDAL` 很容易触发源码编译并安装失败。

## 1. 安装 Conda

先安装 Anaconda 或 Miniconda。

安装完成后，打开 Anaconda Prompt 或 PowerShell。

## 2. 进入项目目录

把命令行切换到 UavTool 项目目录。

示例：

```powershell
cd 你的项目路径\UavTool
```

如果项目在桌面，可以类似这样：

```powershell
cd $HOME\Desktop\UavTool
```

## 3. 创建新环境并安装 GDAL

创建名为 `UavTool` 的 Conda 环境，并安装 Python、GDAL、pyproj、NumPy：

```powershell
conda create -n UavTool -c conda-forge python=3.11 gdal=3.10.3 pyproj=3.7.1 numpy=2.2.6 pip -y
```

激活环境：

```powershell
conda activate UavTool
```

## 4. 安装剩余 Python 包

在已经激活的 `UavTool` 环境中执行：

```powershell
python -m pip install PySide6==6.8.3 pyinstaller==6.19.0 pyinstaller-hooks-contrib==2026.4 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

说明：

- `GDAL`、`pyproj`、`numpy` 已经由 Conda 安装。
- `PySide6`、`PyInstaller` 由 pip 安装。
- 当前项目推荐使用 `PySide6==6.8.3`。

## 5. 验证环境

执行：

```powershell
python -c "from osgeo import gdal; import pyproj, numpy; from PySide6.QtWidgets import QApplication; print(gdal.VersionInfo('--version')); print(pyproj.__version__); print(numpy.__version__); print('ok')"
```

如果输出包含 `ok`，说明核心依赖可用。

再检查依赖是否冲突：

```powershell
python -m pip check
```

正常输出：

```text
No broken requirements found.
```

## 6. 运行项目

确认当前已经激活环境：

```powershell
conda activate UavTool
```

运行：

```powershell
python main.py
```

## 7. 常见问题

### pip 安装 GDAL 失败

如果看到类似错误：

```text
Getting requirements to build wheel did not run successfully
gdal-3.10.3.tar.gz
```

说明 pip 正在尝试编译 GDAL。请使用本文第 3 步的 Conda 命令安装 GDAL。

### PySide6 报 DLL load failed

如果看到类似错误：

```text
ImportError: DLL load failed while importing QtWidgets
```

重新安装当前推荐版本：

```powershell
python -m pip install --force-reinstall PySide6==6.8.3 pyinstaller==6.19.0 pyinstaller-hooks-contrib==2026.4 -i https://pypi.tuna.tsinghua.edu.cn/simple
```

## 8. 当前推荐版本

```text
GDAL==3.10.3
numpy==2.2.6
pyproj==3.7.1
PySide6==6.8.3
PySide6_Addons==6.8.3
PySide6_Essentials==6.8.3
shiboken6==6.8.3
pyinstaller==6.19.0
pyinstaller-hooks-contrib==2026.4

```
