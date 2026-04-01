import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz, process
import re

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

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

def clean_desc(series):
    return series.astype(str).str.lower().str.strip()

def generate_keys(df, col, prefix):
    s = clean_upc(df[col])
    df[f"{prefix}_12"] = s.str.zfill(12)
    df[f"{prefix}_10"] = df[f"{prefix}_12"].str[-10:]

# ==============================
# NEW: PACK PARSER
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
# NEW: BRAND DETECTION
# ==============================
def build_brand_list(df, product_desc):
    phrases = df[product_desc].astype(str).str.lower().str.split().str[:3].str.join(" ")
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
st.header("Upload Files")

adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store Assignment File", type=["xlsx"])

if adm_file and product_file and store_file:

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    st.success("Files loaded")

    # ==============================
    # COLUMN SELECTORS
    # ==============================
    st.header("Select Columns")

    col1, col2, col3 = st.columns(3)

    with col1:
        main_upc = st.selectbox("Main UPC", main_df.columns)
        main_desc = st.selectbox("Main Description", main_df.columns)
        main_store = st.selectbox("Main Store", main_df.columns)

    with col2:
        product_upc1 = st.selectbox("Product UPC 1", product_df.columns)
        product_upc2 = st.selectbox("Product UPC 2", product_df.columns)
        product_desc = st.selectbox("Product Description", product_df.columns)
        product_uid = st.selectbox("Product UID", product_df.columns)
        product_family = st.selectbox("Product Family", product_df.columns)

    with col3:
        sf_store = st.selectbox("Store Column", sf_df.columns)
        sf_family = st.selectbox("Family Column", sf_df.columns)

    st.info("Select columns, then click Process")

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # CLEAN
        status.text("🔄 Cleaning data...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])
        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        # MATCH
        status.text("🔎 Matching...")
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

        def fuzzy_match(row):
            if isinstance(row[product_uid], list):
                return row[product_uid], row[product_family], "UPC Match"

            upc10 = row["m_10"]
            candidates = product_df[product_df["p_12"].astype(str).str.contains(upc10, na=False)]

            if candidates.empty:
                return None, None, "No Match"

            return list(set(candidates[product_uid])), list(set(candidates[product_family])), "Partial"

        res = merged.apply(fuzzy_match, axis=1)

        merged["Retail UID"] = res.apply(lambda x: x[0][0] if isinstance(x[0], list) else None)
        merged["Family"] = res.apply(lambda x: x[1][0] if isinstance(x[1], list) else None)
        merged["Match Type"] = res.apply(lambda x: x[2])

        progress.progress(60)

        # STORE VALIDATION
        merged["store_family_key"] = merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
        sf_df["store_family_key"] = sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)

        merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

        # NEW: REASON TAG
        def get_reason(row):
            if pd.isna(row["Retail UID"]) and not row["Valid Store-Family"]:
                return "No Match + Invalid Store-Family"
            elif pd.isna(row["Retail UID"]):
                return "No Match"
            elif not row["Valid Store-Family"]:
                return "Invalid Store-Family"
            return "Good"

        merged["Reason"] = merged.apply(get_reason, axis=1)

        progress.progress(80)

        # OUTPUT
        good_df = merged[merged["Reason"] == "Good"][[main_store, "Retail UID"]].drop_duplicates()
        invalid_df = merged[merged["Reason"] != "Good"][[main_store, main_upc, main_desc, "Reason"]]

        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates()

        unmatched_df.columns = ["UPC", "Description"]

        # NEW: INFERENCE
        brand_list = build_brand_list(product_df, product_desc)

        def infer(desc):
            group, size, unit = parse_pack(desc)
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

        if not unmatched_df.empty:
            unmatched_df = pd.concat([unmatched_df, unmatched_df["Description"].apply(infer)], axis=1)

        # NEW: TEMPLATE
        template = pd.DataFrame({
            "ProductId": unmatched_df["UPC"],
            "Product Name": unmatched_df["Description"],
            "Group": unmatched_df.get("Group"),
            "ProductUPC": unmatched_df["UPC"],
            "UnitUPC": unmatched_df["UPC"],
            "CaseUPC": unmatched_df["UPC"],
            "Active": "true",
            "Products/Case": unmatched_df.get("Products/Case"),
            "Units/Product": unmatched_df.get("Units/Product"),
            "Unit Size": unmatched_df.get("Unit Size"),
            "Unit Measure": unmatched_df.get("Unit Measure"),
            "Family Head": "false"
        })

        # EXPORT
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            pd.DataFrame({"Status":["OK"]}).to_excel(writer, "Status", index=False)
            merged.to_excel(writer, "Full Output", index=False)
            good_df.to_excel(writer, "Good To Go", index=False)
            invalid_df.to_excel(writer, "Invalid", index=False)
            unmatched_df.to_excel(writer, "Unmatched", index=False)
            template.to_excel(writer, "Product Template", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("✅ Done!")

        st.download_button("📥 Download", output, "processed.xlsx")
