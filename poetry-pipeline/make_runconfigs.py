# -*- coding: utf-8 -*-
"""生成 PyCharm 的 .idea/runConfigurations/*.xml，省掉手工配 5 个入口的麻烦。

**必须写到项目根的 .idea**，不能写 poetry-pipeline/.idea：PyCharm 打开的项目根是
E:\\chinese_poetry_iterate（README §零 有说明），它只加载根目录下的 .idea/runConfigurations，
子树里的 .idea 是死文件——配置写进去不会报错，只是永远不会出现在运行配置下拉里。

（早先版本确实写进 poetry-pipeline/.idea，理由是不想污染源库自带的 .idea。但源库的 .idea
本就被根 .gitignore 忽略，且这里已放行 runConfigurations 子目录，污染问题不存在，
配置不能生效才是真问题。）

路径一律用 $PROJECT_DIR$/poetry-pipeline/...，对应「PyCharm 打开 E:\\chinese_poetry_iterate
作为项目根」的用法。若直接把 poetry-pipeline 当项目根，这些配置的路径会失效，
改为手工指定即可。
"""
from __future__ import annotations
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, os.pardir, ".idea", "runConfigurations")
CFG = "$PROJECT_DIR$/poetry-pipeline"

# name, script(相对工程), parameters
ITEMS = [
    ("1 Build 全量", "build.py", "--with-strains"),
    # Verify 的两个参数不能省：verify_db.py 的默认值只解析主库，平仄包默认 ""，
    # 不传就跳过「平仄包按 id 全部可关联主库」那条断言，而 README §校验 要求它全绿。
    ("2 Verify 校验", "verify_db.py", "dist/poetry.db dist/poetry-strains.db"),
    # bench 挂上平仄库才测得到那部分查询路径（README 2.1 的延迟表就在这个配置下测的）。
    ("3 Query 基准", "query.py", "--strains-db dist/poetry-strains.db bench"),
    ("4 App 桌面窗口", "app.py", ""),
    ("5 API 服务", "examples/api_server.py", "--allow-write --port 8787"),
    ("6 Build 冒烟", "build.py", "--out ./dist-smoke --limit 3000"),
]

TPL = """<component name="ProjectRunConfigurationManager">
  <configuration default="false" name="{name}" type="PythonConfigurationType" factoryName="Python" nameIsGenerated="false">
    <option name="INTERPRETER_OPTIONS" value="" />
    <option name="PARENT_ENVS" value="true" />
    <option name="SDK_HOME" value="{cfg}/.venv/Scripts/python.exe" />
    <option name="WORKING_DIRECTORY" value="{cfg}" />
    <option name="IS_MODULE_SDK" value="false" />
    <option name="ADD_CONTENT_ROOTS" value="true" />
    <option name="ADD_SOURCE_ROOTS" value="true" />
    <option name="SCRIPT_NAME" value="{cfg}/{script}" />
    <option name="PARAMETERS" value="{params}" />
    <option name="SHOW_COMMAND_LINE" value="false" />
    <option name="EMULATE_TERMINAL" value="false" />
    <option name="MODULE_MODE" value="false" />
    <option name="REDIRECT_INPUT" value="false" />
    <option name="ELEVATE" value="false" />
    <method v="2" />
  </configuration>
</component>
"""

os.makedirs(OUT, exist_ok=True)
for name, script, params in ITEMS:
    fn = name.replace(" ", "_") + ".xml"
    with open(os.path.join(OUT, fn), "w", encoding="utf-8") as f:
        f.write(TPL.format(name=name, cfg=CFG, script=script, params=params))
    print("写入", fn)
print("目录:", OUT)
