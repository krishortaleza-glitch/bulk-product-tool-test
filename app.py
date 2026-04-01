import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz, process
import re

st.set_page_config(page_title="Bulk Product Tool", layout="wide")
st.title("📦 Bulk Product Tool")

@st.cache_data
def load_file(file):
    return pd.read_excel(file)

# ==============================
# HELPERS
# ==============================
def clean_upc(series):
    return (
        series.astype(str)
        .str.replace(r"\.0$", "", regex=True)
        .str.replace(r"\D", "", regex=True)
    )

def generate_keys(df, col, prefix):
    s = clean_upc(df[col])
    df[f"{prefix}_12"] = s.str.zfill(12)
    df[f"{prefix}_10"] = df[f"{prefix}_12"].str[-10:]

# ==============================
# PACK PARSER
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()
    group, size, unit = None, None, None

    m = re.search(r"(\d+)/(\d+)(oz|ml)?", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = int(m.group(2))
        if m.group(3):
            unit = m.group(3).upper()

    m = re.search(r"(\d+)pk.*?(\d+)(oz|ml)", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = int(m.group(2))
        unit = m.group(3).upper()

    return group, size, unit

# ==============================
# BRAND DETECTION
# ==============================
def build_brand_list(df):
    phrases = df["Product Name"].astype(str).str.lower().str.split().str[:3].str.join(" ")
    return phrases.value_counts().head(200).index.tolist()

def detect_brand(desc, brand_list):
    desc = str(desc).lower()
    for b in sorted(brand_list, key=len, reverse=True):
        if b in desc:
            return b
    return None

# ==============================
# UI
# ==============================
adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store File", type=["xlsx"])

if adm_file and product_file and store_file:

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    product_df.columns = product_df.columns.str.strip()

    # ==============================
    # SAFE UPC EXPLODE (OLD LOGIC)
    # ==============================
    df1 = product_df.copy()
    df1["UPC_list"] = df1["ProductUPC"]

    df2 = product_df.copy()
    df2["UPC_list"] = df2["UnitUPC"]

    product_df = pd.concat([df1, df2], ignore_index=True)
    product_df = product_df.dropna(subset=["UPC_list"])

    # ==============================
    # KEYS
    # ==============================
    generate_keys(main_df, main_df.columns[0], "m")
    generate_keys(product_df, "UPC_list", "p")

    product_df["p_12"] = product_df["p_12"].fillna("")

    # ==============================
    # MATCHING (RESTORED)
    # ==============================
    map_12 = product_df.groupby("p_12").agg({
        "ProductId": lambda x: list(set(x)),
        "Family": lambda x: list(set(x))
    })

    merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

    def match(row):
        if isinstance(row["ProductId"], list):
            return row["ProductId"], row["Family"], "UPC Match"

        upc10 = row["m_10"]
        c = product_df[product_df["p_12"].str.contains(upc10)]

        if c.empty:
            return None, None, "No Match"

        return list(set(c["ProductId"])), list(set(c["Family"])), "Partial"

    res = merged.apply(match, axis=1)

    merged["Retail UID"] = res.apply(lambda x: x[0][0] if isinstance(x[0], list) else None)
    merged["Family"] = res.apply(lambda x: x[1][0] if isinstance(x[1], list) else None)
    merged["Match Type"] = res.apply(lambda x: x[2])

    # ==============================
    # STORE VALIDATION
    # ==============================
    merged["store_family_key"] = merged.iloc[:,2].astype(str) + "|" + merged["Family"].astype(str)
    sf_df["store_family_key"] = sf_df.iloc[:,0].astype(str) + "|" + sf_df.iloc[:,1].astype(str)

    merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

    # ==============================
    # UNMATCHED + SMART LOGIC
    # ==============================
    brand_list = build_brand_list(product_df)

    unmatched = merged[merged["Match Type"] == "No Match"][
        [main_df.columns[0], main_df.columns[1]]
    ].drop_duplicates()

    unmatched.columns = ["UPC", "Description"]

    def infer(desc):
        group, size, unit = parse_pack(desc)
        brand = detect_brand(desc, brand_list)

        rows = product_df[product_df["Group"] == group]

        def mode(col):
            return rows[col].mode().iloc[0] if col in rows and not rows[col].mode().empty else None

        return pd.Series({
            "Group": group,
            "Products/Case": mode("Products/Case"),
            "Units/Product": mode("Units/Product"),
            "Unit Size": size or mode("Unit Size"),
            "Unit Measure": unit or mode("Unit Measure"),
        })

    if not unmatched.empty:
        unmatched = pd.concat([unmatched, unmatched["Description"].apply(infer)], axis=1)

    # ==============================
    # TEMPLATE
    # ==============================
    template = pd.DataFrame({
        "ProductId": unmatched["UPC"],
        "Product Name": unmatched["Description"],
        "Group": unmatched.get("Group"),
        "ProductUPC": unmatched["UPC"],
        "UnitUPC": unmatched["UPC"],
        "CaseUPC": unmatched["UPC"],
        "Active": "true",
        "Products/Case": unmatched.get("Products/Case"),
        "Units/Product": unmatched.get("Units/Product"),
        "Unit Size": unmatched.get("Unit Size"),
        "Unit Measure": unmatched.get("Unit Measure"),
        "Family Head": "false"
    })

    # ==============================
    # EXPORT (NO CRASH)
    # ==============================
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        pd.DataFrame({"Status":["OK"]}).to_excel(writer, "Status", index=False)
        merged.to_excel(writer, "Full Output", index=False)
        unmatched.to_excel(writer, "Unmatched", index=False)
        template.to_excel(writer, "Product Template", index=False)

    output.seek(0)

    st.download_button("📥 Download", output, "output.xlsx")
