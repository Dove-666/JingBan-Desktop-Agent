# SAM2 角色动画素材管线

这里处理**已经存在的视频**，不会生成或提交视频，也不使用色键算法替代 SAM2。
原始 `assets/whale.png` 保留不变。静态素材的处理见 `static_assets.py`。

## 本机已验证的独立环境

2026-10-30 实际使用 `.venv-assets`，复用既有 Conda 的 Python 3.14.6、
Torch 2.14.0+cu130 和 RTX 4060 Laptop GPU；没有在桌宠 `.venv` 中安装 Torch，
没有重新下载几 GB 的 Torch。新环境文件约 23.9 MiB，官方 tiny checkpoint
148.8 MiB，pip 缓存约 3.5 MiB。具体版本和字节数见
`artifacts/asset_pipeline/environment_report.json`。

Meta 官方代码固定为 commit `2b90b9f5ceec907a1c18123530e92e794ad901a4`，
位于 `artifacts/asset_pipeline/vendor/meta-sam2`。checkpoint 为
`artifacts/asset_pipeline/checkpoints/sam2.1_hiera_tiny.pt`，实际下载大小
156008466 bytes，本机计算 SHA256：
`7402e0d864fa82708a20fbd15bc84245c2f26dff0eb43a4b5b93452deb34be69`。

该环境依赖本机现有 Conda Torch；复制仓库不会复制此环境。重新搭建时先检查
Python/Torch/CUDA 是否可用，再选择适配的 Torch，避免 pip 自动替换大依赖。
`requirements-assets.txt` 只描述可选素材工具依赖；不属于桌宠启动依赖。
它锁定本机实际验证的 Python 3.14.6 / Torch 2.14.0 环境，不代表这些版本
适配桌宠 Python 3.10 或任意旧 Torch；其他机器须先选择相互适配的版本。

```powershell
# 已有兼容 Torch 的 Python 创建独立环境，示例路径需要换为自己的安装位置。
& 'C:\Path\To\Conda\python.exe' -m venv --system-site-packages .venv-assets
$env:PIP_CACHE_DIR = "$PWD\artifacts\asset_pipeline\cache\pip"
$env:HF_HOME = "$PWD\artifacts\asset_pipeline\cache\huggingface"
$env:TORCH_HOME = "$PWD\artifacts\asset_pipeline\cache\torch"
$assetTemp = "$PWD\artifacts\asset_pipeline\cache\temp"
New-Item -ItemType Directory -Force -Path $assetTemp | Out-Null
$env:TEMP = $assetTemp
$env:TMP = $assetTemp
.\.venv-assets\Scripts\python.exe -m pip install --no-deps -r requirements-assets.txt

# 只从 Meta 官方仓库取得源代码；不会安装新的 Torch。
git clone --depth 1 --filter=blob:none --sparse https://github.com/facebookresearch/sam2.git artifacts/asset_pipeline/vendor/meta-sam2
git -C artifacts/asset_pipeline/vendor/meta-sam2 fetch --depth 1 origin 2b90b9f5ceec907a1c18123530e92e794ad901a4
git -C artifacts/asset_pipeline/vendor/meta-sam2 checkout FETCH_HEAD
git -C artifacts/asset_pipeline/vendor/meta-sam2 sparse-checkout set sam2
$env:SAM2_BUILD_CUDA = '0'
.\.venv-assets\Scripts\python.exe -m pip install --no-deps --no-build-isolation -e artifacts/asset_pipeline/vendor/meta-sam2
```

本轮实际新增的小依赖安装命令为（其余 Pillow/NumPy/Torch/tqdm 复用原环境）：

```powershell
.\.venv-assets\Scripts\python.exe -m pip install --no-deps torchvision==0.29.2 hydra-core==1.3.2 iopath==0.1.10 omegaconf==2.3.1 antlr4-python3-runtime==4.9.3 PyYAML==6.0.3 portalocker==4.4.2
```

Windows 原生构建没有编译可选 CUDA connected-components 扩展，管线显式关闭该
后处理；**实际 CUDA mask 预测和时序传播已运行成功**。Meta 推荐 Windows
使用 WSL，换机器时仍应重新验证兼容性。

官方 checkpoint 下载地址：
[Meta SAM2.1 tiny](https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_tiny.pt)。
不会在桌宠启动或本工具 import 时自动下载。

## 技术 smoke：没有动作视频时也可以验证真实 SAM2

```powershell
.\.venv-assets\Scripts\python.exe -m tools.asset_pipeline.sam2_smoke --output artifacts/asset_pipeline/sam2_smoke_new
```

输出目录必须不存在。它只把既有原图合成到中性背景，重复 4 帧，然后实际调用
SAM2 的 `init_state`、`add_new_points_or_box` 和 `propagate_in_video`。
参考 alpha 只用于首帧 box 和结果 IoU 评估，输出 mask 由 SAM2 预测，
不从原 alpha 复制。结果不会写入产品动画目录，不生成产品 `animation.json`。

最终代码重跑 `artifacts/asset_pipeline/sam2_smoke_v2/report.json` 显示 4 帧都成功，
用时约 6.74 秒，对原图 alpha 的 IoU 为 0.9871～0.9886，原图 SHA256 未变。
`synthetic-edge-preview.png` 提供浅/深背景检查图。
**这只证明真实 SAM2 API 和固定画布管线可运行，不代表已制作任何所需动作，
也不证明运动视频中的时序稳定性或跨动作比例一致性。该次 smoke 尚未使用真实动作视频；
后续实片结果见文末。**

## 处理真实视频

需要 PATH 中存在 `ffmpeg` 和 `ffprobe`。先查看真实视频首帧，以原始像素坐标给出
包围完整角色的 `LEFT TOP RIGHT BOTTOM`，再执行以下命令；大写坐标是待填写参数。

```powershell
.\.venv-assets\Scripts\python.exe -m tools.asset_pipeline.sam2_video `
  artifacts/asset_pipeline/inputs/think_scratch_head.mp4 `
  artifacts/asset_pipeline/outputs/think_scratch_head_draft `
  --checkpoint artifacts/asset_pipeline/checkpoints/sam2.1_hiera_tiny.pt `
  --name think_scratch_head --box LEFT TOP RIGHT BOTTOM --fps 12 --keep-source-frames

# 第二个视频需要独立检查首帧、填写它自己的 box；不是复用第一个视频坐标。
.\.venv-assets\Scripts\python.exe -m tools.asset_pipeline.sam2_video `
  artifacts/asset_pipeline/inputs/snarl_cute.mp4 `
  artifacts/asset_pipeline/outputs/snarl_cute_draft `
  --checkpoint artifacts/asset_pipeline/checkpoints/sam2.1_hiera_tiny.pt `
  --name snarl_cute --box LEFT TOP RIGHT BOTTOM --fps 12 --keep-source-frames --completion hold_last
```

处理顺序：

1. `ffmpeg -fps_mode passthrough` 解码源视频**每个原生帧**，没有先抽帧。
2. SAM2 对全部原生帧作真实时序分割，使用首帧 box 提示；不会用背景颜色替代分割。
3. 用**全部帧** alpha 的联合 bbox 计算唯一缩放和平移，所有帧共享该变换。
   采用 `assets/character/canvas.json` 的 1024 画布、896 安全范围和底锚点 `(512,960)`。
4. 完成原生逐帧分割后，按源时间戳选取 12 FPS 游戏序列；5 秒通常为 60 帧。
   不插帧、不逐帧放大或重新居中。游戏序列最多 96 帧，超出则明确失败。
5. `origin` 记录源 FPS、原帧数、已分割帧数、交付 FPS、源帧索引和采样策略。
   `--keep-source-frames` 另存全部原生尺寸 RGBA 到 `source_rgba/`；它们不是游戏播放序列。
6. 验证全部交付 PNG 的 alpha、尺寸、边界、mask 面积突变；失败不发布半成品。
   已存在的输出目录不会被覆盖。

**联合 bbox 只保证同一个片段内部不抖动，不保证不同片段角色比例相同。**
每组各自适配 896 安全范围，趴低或收拢身体的动作仍可能被整体放大。
真实两视频到位后，必须将两组首帧站姿、master 和底锚点并排比较，
再检查全段最大伸展姿态；必要时统一减小所有组的全局安全尺寸或调整整组固定比例，
不能逐帧放大补偿。单片段自动 `qa_passed` **不包含跨 clip 视觉尺度验收**；
本批两段的共同标定和人工对比结果见文末实片记录。

`(512,960)` 是变换使用的逻辑底锚点。SAM2 mask 的轻微羽化及 LANCZOS 重采样
会让极软 alpha 延伸到 y=961/962；这与静态图可见边界恰好为 y=960 并不逐像素一致。
QA 允许画布安全范围内的软边像素，仍拒绝触及画布边界的内容；
人工检查应比较主体落脚位置和软边观感，不能把逻辑锚点解释为所有 alpha 像素的硬截断。

结果目录含 `frames/0001.png...`、`animation.json`、`qa.json`。
默认 `complete:false` 是**待检查草稿**，播放器拒绝播放，避免自动检查冒充视觉验收。
人工检查动作、身份、头发/尾巴边缘、浅深背景、整段比例和回待机衔接后，
可在新输出目录重新执行并加 `--reviewed`，再将已审核整目录放入
`assets/character/animations/<name>/`。不要仅因为脚本没报错就勾选审核。

首轮准备时尚无真实视频输入，只有 Mock 流程检查和重复静态帧 GPU smoke；
后续已取得两段真实视频并运行原生全帧分割，结果见文末。

本轮新管线单测 13 项通过；与静态布局 3 项合计 16 项通过：

```powershell
.\.venv\Scripts\python.exe -m unittest tests.test_asset_sequence_pipeline tests.test_static_asset_layout -v
.\.venv\Scripts\python.exe -m py_compile tools/asset_pipeline/sequence_layout.py tools/asset_pipeline/sam2_video.py tools/asset_pipeline/sam2_smoke.py
```

## 后续准备：真实问号与跨片段共同站姿比例

2026-10-30 后续新增可选接口，旧命令保持可用。环境只读复核确认 checkpoint
SHA256 一致、真实 SAM2 可导入、CUDA 仍可用。旧单对象路径实际再次运行
`artifacts/asset_pipeline/sam2_smoke_v3/report.json`，四帧重复原图 fixture
成功，7.047 秒；依然不是动作视频。新旧管线及静态相关单测共 27 项通过。

如果真实源视频中确实出现了独立蓝色问号，可给命令增加
`--effects artifacts/asset_pipeline/inputs/think_effects.json`。该 JSON 是对象数组，
每个对象需要 `name`（例如 `question_mark`）、`frame_idx`、`start_frame`、
`end_frame`、`box`。帧索引是**原视频零基原生索引**，box 是对应源帧像素坐标。
应先看片再填写：`frame_idx` 选择轮廓清楚的一帧，起止区间限制问号真实可见范围。
还可填写 `points:[[x,y],...]` 和同长度 `labels:[1,0,...]`，分别表示物体正点和
背景负点；问号弯钩与圆点可分别给正点。不要凭空配置视频没有的问号。

问号作为独立 SAM2 object 分割，显隐范围外强制透明，范围内只采用真实 SAM2
预测；最终 PNG 的 alpha 是角色和效果的并集。加 `--keep-source-frames` 时，
除了 `source_rgba/`，还保留 `object_layers/character/` 和
`object_layers/question_mark/` 的全部原生帧。这些层为后续检查和身高标定提供依据。
这些自动测试覆盖提示校验、显隐和几何；之后的真实问号检查及估计透明淡出见下文。

待两组真实视频分别分割并保留原生 RGBA 后，用各自**站立**参考帧标定：

```powershell
.\.venv-assets\Scripts\python.exe -m tools.asset_pipeline.shared_layout calibrate `
  --clip think_scratch_head artifacts/asset_pipeline/outputs/think_scratch_head_draft 0 `
  --clip snarl_cute artifacts/asset_pipeline/outputs/snarl_cute_draft 0 `
  --output artifacts/asset_pipeline/outputs/common_layout.json
```

最后的 `0` 是待人工核对的原生站立帧索引，不应在首帧已趴低时照抄。
工具从 `object_layers/character` 取站姿人体框，排除漂浮问号；没有效果时可用
原生 RGBA。两组先按站姿高度匹配 master，再根据两段全部帧的角色+效果联合边界
计算**共同**安全缩小系数，脚点都对齐逻辑 y=960。它不会把趴低的动作逐帧放大。

```powershell
.\.venv-assets\Scripts\python.exe -m tools.asset_pipeline.shared_layout repack `
  artifacts/asset_pipeline/outputs/think_scratch_head_draft `
  artifacts/asset_pipeline/outputs/think_scratch_head_shared_draft `
  --plan artifacts/asset_pipeline/outputs/common_layout.json --name think_scratch_head
```

第二组同理使用自己的源目录和 `--name snarl_cute`。该步骤只重用已经实际分割的
RGBA，不重复 SAM2 推理、不重新请求视频；原有产物不覆盖、不删除。
默认仍是 `complete:false`，并排检查主体身高、底部、动作边缘和回待机衔接后
才使用 `--reviewed`。如果源视频自身改变角色头身比例或镜头透视，固定变换不能
修复形变，应诚实记录限制，不能宣称数值校准就完成了视觉一致性。

## 两段真实视频的实际处理结果

实际收到 AI派 / MiniMax-H3 的两个不同输出，源文件保留在
`artifacts/asset_pipeline/inputs/think_scratch_head_download.mp4` 和
`snarl_cute_download.mp4`。每段视频流均为 2560×1440、24 FPS、124 原生帧、
5.166667 秒；网页选择 5 秒但服务实际返回略长。第二段另有 5.267344 秒音轨，
本管线按**视频流**时长处理，未采用音轨长度，也未擅自裁成 5 秒。

两段全部 **248 个原生帧实际经过 SAM2 CUDA 预测**；不是先抽取 12 FPS 才分割。
随后按每两个原生帧取一帧派生，各交付 62 帧 / 12 FPS / 5167 ms。
`delivery_indices` 已针对 ffprobe 六位小数时长添加容差，避免 62.000004 被误算成 63 帧。

原始分割草稿与原生层一直保留：

- `artifacts/asset_pipeline/outputs/think_scratch_head_native_draft_v1/`：124 个合成 RGBA、124 个角色层和 124 个问号层。
- `artifacts/asset_pipeline/outputs/snarl_cute_native_draft_v1/`：124 个角色 RGBA；本段没有独立漂浮效果。
- `artifacts/asset_pipeline/inspect_think_v1/real_sam2_report.json` 和 `inspect_snarl_v1/real_sam2_report.json`：实片报告及浅/深背景 contact sheet。

问号采用真实 SAM2 独立 object，在原生 48 帧给 box 和弯钩、圆点正点。
原生 45～99 帧有独立 mask，最早极小淡入的 44 帧未被独立分出，保留该限制，
没有补画。多对象 mask 会重叠，原始角色 mask 也可能包含同一个问号。
源视频把淡出编码成黑底 RGB 变暗；直接抠图会在白底留下黑问号。
经浅/深背景预览确认，另外生成了**估计 alpha**版本：以 45～85 帧问号内部
蓝色 P90 的中位数 250 为参考，逐帧估计淡出系数，RGB 除该系数、alpha 乘该系数。
轮廓仍完全来自真实 SAM2；角色重叠仅在独立问号 mask 支持膨胀 2 像素内移除，
邻近蝴蝶结保留。原始视频和第一次分割结果没有修改。

这不是源视频自带的 alpha，也无法恢复极暗压缩像素的精确原色。
方法、逐帧系数、源码 checkpoint SHA 和生成服务追溯均写入最终 `origin` / `qa`。
独立派生脚本和试验记录保留在 `artifacts/asset_pipeline/`，没有改写冻结的产品管线：
`estimate_question_fade_trial_v2.py`、`derive_think_fade_and_shared_ready_v1.py`。
全部 124 组修正原生层保留在 `outputs/think_scratch_head_estimated_alpha_native_v2/`。

共同标定固定使用 **未二次缩小的**
`artifacts/asset_pipeline/master_baseline_for_calibration_v1.png`，参考可见高度 898。
`outputs/common_layout_v1.json` 根据两段全部帧联合边界，得到共同安全系数
`0.7412408317219669`、目标站姿高度 `665.6342668863263`。
两段首帧 opaque alpha>128 主体高度实际均为 665 像素；软 alpha>1 包含羽化和
重采样，分别为 670/669，底部可到 y=962。静态 master/tsundere 也由保留源
按相同系数重新规范化，主体约 667/666 像素，逻辑底锚点仍是 y=960。
不要拿已经缩小的新 master 再次计算系数，避免重复缩小。

最终经整段及淡出预览检查的新目录为：

- `artifacts/asset_pipeline/outputs/think_scratch_head_shared_ready_v1/`：完成后回待机，`completion: idle`。
- `artifacts/asset_pipeline/outputs/snarl_cute_shared_ready_v1/`：完成后保持最后一帧，`completion: hold_last`。

各目录 62 张 PNG 全部验证为 RGBA / 1024×1024 / 非空角色 / 安全透明边界，
`complete:true`、`qa_passed:true`。最终交付与共同站姿对比见
`artifacts/asset_pipeline/final_shared_qa_v1/final_report.json`、
`standing_scale_contact.png`、`both_full_clip_contact.png`。
正式复制至 `assets/character/animations/<name>/` 时仍须确认目标不存在，保留来源。

最后管线/静态相关 29 项测试通过；整项目由主任务运行 640 项通过、0 跳过、
0 失败，用时 237.607 秒，记录
`artifacts/character_assets_real_v1/unittest_report.json`。
固定变换能统一站姿尺度与锚点，不能修复生成视频自身的头身比例、透视变化；
蹲伏动作保持其真实原生变化，没有逐帧重新放大。

## 官方接口依据

- [Meta SAM2 官方 README](https://github.com/facebookresearch/sam2)：安装条件、checkpoint、Windows/WSL 和视频示例。
- [官方 build_sam.py](https://github.com/facebookresearch/sam2/blob/main/sam2/build_sam.py)：`build_sam2_video_predictor`。
- [官方 SAM2VideoPredictor](https://github.com/facebookresearch/sam2/blob/main/sam2/sam2_video_predictor.py)：真实视频预测接口。
- [官方 misc.py](https://github.com/facebookresearch/sam2/blob/main/sam2/utils/misc.py)：数字命名 JPEG 目录和 CPU offload。
- [官方 setup.py](https://github.com/facebookresearch/sam2/blob/main/setup.py)：`SAM2_BUILD_CUDA=0` 可选扩展。
