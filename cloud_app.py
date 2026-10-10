"""Streamlit Community Cloud入口；平台Secrets从环境读取，不读取作者.env。"""
import os

import streamlit as st


def load_platform_settings():
    os.environ["FINANCE_CLOUD"] = "1"
    # 根级Secrets本身会导出为环境变量；显式加载也兼容本地模拟入口。
    keys = ("TEAM_ACCESS_CODE", "FINANCE_RUNTIME_DIR", "DEEPSEEK_API_KEY",
            "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL", "DEEPSEEK_TIMEOUT", "MAX_MODEL_CALLS",
            "MAX_OUTPUT_TOKENS", "DEEPSEEK_TEMPERATURE")
    try:
        for key in keys:
            if key not in os.environ and key in st.secrets:
                os.environ[key] = str(st.secrets[key])
    except FileNotFoundError:
        # 未配置Secrets时由页面给出缺访问口令的提示，并阻止访问。
        pass
    finally:
        # Streamlit加载根级Secrets时会自动导出环境变量；入口不能被误配置切成本地。
        os.environ["FINANCE_CLOUD"] = "1"


if __name__ == "__main__":
    load_platform_settings()
    from app import main
    main()
