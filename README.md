### 烟雨江湖自用助手

自用喵，看得懂算你厉害喵

## 声明

使用本项目的任何代码或者由本项目生成的二进制文件都视为已同意本声明以及LICENSE协议。

## 使用规约

1.  本项目是基于学术交流目的建立，仅供交流与学习使用，不得用于商业用途，同时因使用本项目而造成的任何损失和影响都与本项目贡献者无关，用户需对自己的操作负责。
2.  将该项目用于任何途径造成的任何损失以及任何责任均由自己承担，项目拥有者不负任何责任。
3.  本项目为开源项目，项目的贡献者不知道所有用户的输入，因此不负责任何用户的输入。
4.  不可用本项目进行违反民法典以及刑法的相关活动。
5.  禁止使用该项目从事违法行为与宗教、政治等活动，该项目维护者坚决抵制上述行为，不同意此条则禁止使用该项目。
6.  使用本项目的任何代码或者二进制文件都视为已同意本声明以及LICENSE文件声明的协议，本仓库 README 已进行劝导义务，不对后续可能存在问题负责。
7.  任何基于本项目制作的视频都必须在简介中声明项目来源。如果将此项目用于任何其他企划，请提前联系并告知本仓库作者。



## For AI Assistant

### 项目推荐结构：

my_pyside6_app/
│
├── main.py                 # 程序入口
├── requirements.txt        # 依赖列表
├── README.md               # 项目说明
│
├── app/                    # 核心应用代码
│   ├── __init__.py
│   ├── core/               # 核心逻辑（业务、工具、配置）
│   │   ├── __init__.py
│   │   ├── config.py       # 全局配置
│   │   ├── logger.py       # 日志工具
│   │   └── utils.py        # 通用工具函数
│   │
│   ├── models/             # 数据模型（MVC/MVVM 中的 Model）
│   │   ├── __init__.py
│   │   └── user_model.py
│   │
│   ├── views/              # 界面文件（MVC/MVVM 中的 View）
│   │   ├── __init__.py
│   │   ├── main_window.py  # 主窗口类
│   │   └── widgets/        # 自定义控件
│   │       └── custom_button.py
│   │
│   ├── controllers/        # 控制器（MVC 中的 Controller / MVVM 中的 ViewModel）
│   │   ├── __init__.py
│   │   └── main_controller.py
│   │
│   └── resources/          # 静态资源
│       ├── icons/
│       ├── images/
│       └── qss/            # 样式表
│
├── ui/                     # Qt Designer 生成的 .ui 文件
│   ├── main_window.ui
│   └── ...
│
└── tests/                  # 单元测试
    ├── __init__.py
    └── test_main.py

