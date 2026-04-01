import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz
import re

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

# ==============================
# LOAD
# ==============================
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

def safe_mode(df, col):
    if col not in df.columns or df.empty:
        return ""
    if df[col].mode().empty:
        return ""
    return df[col].mode().iloc[0]

# ==============================
# PACK PARSER
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()
    group, size, unit = "", "", ""

    m = re.search(r"(\d+)\s*/\s*(\d+)(oz|ml)?", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = m.group(2)
        unit = (m.group(3) or "").upper()

    m = re.search(r"(\d+)\s*pk.*?(\d+)(oz|ml)", desc)
    if m:
        group = f"{m.group(1)}pk"
        size = m.group(2)
        unit = m.group(3).upper()

    return group, size, unit

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

    product_df.columns = product_df.columns.str.strip()

    st.success("Files loaded")

    # ==============================
    # COLUMN SELECTORS
    # ==============================
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

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # ==============================
        # CLEAN
        # ==============================
        status.text("Cleaning...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])
        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        # ==============================
        # MATCH
        # ==============================
        status.text("Matching...")

        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

        def fuzzy_match(row):
            if isinstance(row[product_uid], list):
                return row[product_uid], row[product_family], "UPC Match"

            upc10 = row["m_10"]

            candidates = product_df[
                product_df["p_12"].astype(str).str.contains(upc10, na=False)
            ]

            if candidates.empty:
                return None, None, "No Match"

            return list(set(candidates[product_uid])), list(set(candidates[product_family])), "Partial"

        res = merged.apply(fuzzy_match, axis=1)

        merged["Retail UID"] = res.apply(lambda x: x[0][0] if isinstance(x[0], list) else None)
        merged["Family"] = res.apply(lambda x: x[1][0] if isinstance(x[1], list) else None)
        merged["Match Type"] = res.apply(lambda x: x[2])

        progress.progress(60)

        # ==============================
        # STORE VALIDATION
        # ==============================
        merged["store_family_key"] = merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
        sf_df["store_family_key"] = sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)
        merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

        # ==============================
        # REASON TAGGING
        # ==============================
        def get_reason(row):
            if pd.isna(row["Retail UID"]) and not row["Valid Store-Family"]:
                return "No Match + Invalid Store-Family"
            elif pd.isna(row["Retail UID"]):
                return "No Match"
            elif not row["Valid Store-Family"]:
                return "Invalid Store-Family"
            return "Good"

        merged["Reason"] = merged.apply(get_reason, axis=1)

        progress.progress(75)

        # ==============================
        # UNMATCHED (DEDUPED)
        # ==============================
        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates()

        unmatched_df.columns = ["UPC", "Description"]

        # ==============================
        # STEP 5: PARSE PACK
        # ==============================
        pack = unmatched_df["Description"].apply(parse_pack)
        unmatched_df["Group"] = pack.apply(lambda x: x[0])
        unmatched_df["Unit Size"] = pack.apply(lambda x: x[1])
        unmatched_df["Unit Measure"] = pack.apply(lambda x: x[2])

        # ==============================
        # STEP 6: INFER CONFIG
        # ==============================
        def infer_config(row):
            group = row.get("Group", "")
            size = str(row.get("Unit Size", ""))

            candidates = product_df.copy()

            if "Group" in candidates.columns:
                candidates = candidates[candidates["Group"] == group]

            if "Unit Size" in candidates.columns and size:
                candidates = candidates[candidates["Unit Size"].astype(str) == size]

            return pd.Series({
                "Products/Case": safe_mode(candidates, "Products/Case"),
                "Units/Product": safe_mode(candidates, "Units/Product"),
            })

        config = unmatched_df.apply(infer_config, axis=1)
        unmatched_df["Products/Case"] = config["Products/Case"]
        unmatched_df["Units/Product"] = config["Units/Product"]

        # ==============================
        # PRODUCT TEMPLATE
        # ==============================
        template_df = pd.DataFrame({
            "ProductId": unmatched_df["UPC"],
            "UnitId": "",
            "CaseId": "",
            "Product Name": unmatched_df["Description"],
            "Type": "",
            "Family": "",
            "Group": unmatched_df["Group"],
            "ProductUPC": unmatched_df["UPC"],
            "UnitUPC": "",
            "CaseUPC": "",
            "Active": "true",
            "Products/Case": unmatched_df["Products/Case"],
            "Units/Product": unmatched_df["Units/Product"],
            "Unit Size": unmatched_df["Unit Size"],
            "Unit Measure": unmatched_df["Unit Measure"],
            "ParentId": "",
            "Family Head": "false"
        })

        # ==============================
        # EXPORT (SAFE)
        # ==============================
        output = BytesIO()

        with pd.ExcelWriter(output, engine="openpyxl") as writer:

            wrote = False

            if not merged.empty:
                merged.to_excel(writer, "Full Output", index=False)
                wrote = True

            if not unmatched_df.empty:
                unmatched_df.to_excel(writer, "Unmatched", index=False)
                wrote = True

            if not template_df.empty:
                template_df.to_excel(writer, "Product Template", index=False)
                wrote = True

            if not wrote:
                pd.DataFrame({"Message": ["No data"]}).to_excel(writer, "Empty", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("Done!")

        st.download_button(
            "📥 Download",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
