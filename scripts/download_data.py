"""下载 H&M 电商数据集(原始 parquet 文件)。

数据来源:
- H&M Personalized Fashion Recommendations (Kaggle 官方竞赛数据, 供分析研究使用)
- 微软 HuggingFace 镜像: https://huggingface.co/datasets/microsoft/hnm-search-data

用法:
    python scripts/download_data.py
"""

from pathlib import Path
from urllib.request import urlretrieve

RAW_DIR = Path(__file__).resolve().parent.parent / "data" / "raw"

FILES = {
    "transactions_train.parquet": "https://huggingface.co/datasets/microsoft/hnm-search-data/resolve/main/data/processed/transactions_train.parquet",
    "customers.parquet": "https://huggingface.co/datasets/microsoft/hnm-search-data/resolve/main/data/processed/customers.parquet",
    "articles.parquet": "https://huggingface.co/datasets/microsoft/hnm-search-data/resolve/main/data/processed/articles.parquet",
}


def main() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    for name, url in FILES.items():
        target = RAW_DIR / name
        if target.exists():
            print(f"已存在,跳过: {target} ({target.stat().st_size / 1024 / 1024:.1f} MB)")
            continue
        print(f"下载中: {name}")
        urlretrieve(url, target)
        print(f"完成: {name} ({target.stat().st_size / 1024 / 1024:.1f} MB)")


if __name__ == "__main__":
    main()
