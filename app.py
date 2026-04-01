import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz
import re

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

# ==============================
# CACHE
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

# ==============================
# EXTRACT SIZE + UNIT
# ==============================
def extract_size_unit(desc):
    desc = str(desc).lower()

    match = re.search(r"(\d+)\s?(oz|ml)", desc)
    if match:
        return int(match.group(1)), match.group(2).upper()

    return None, None

# ==============================
# SMART ATTRIBUTE INFERENCE
# ==============================
def infer_attributes_full(desc, product_df, product_desc):
    desc_clean = str(desc).lower()
    brand = desc_clean.split()[0] if desc_clean else ""

    size, unit_measure = extract_size_unit(desc)

    candidates = product_df[
        product_df[product_desc].astype(str).str.lower().str.startswith(brand, na=False)
    ].copy()

    if candidates.empty:
        return {}

    candidates["score"] = candidates[product_desc].apply(
        lambda x: fuzz.partial_ratio(desc_clean, str(x).lower())
    )

    top = candidates[candidates["score"] >= 75]

    if top.empty:
        return {}

    def safe_mode(df, col):
        return df[col].mode().iloc[0] if col in df and not df[col].mode().empty else None

    # Step 1: infer group
    group = safe_mode(top, "Group")

    # Step 2: try exact match (group + size)
    if size:
        exact = top[
            (top["Group"] == group) &
            (top["Unit2"] == size)
        ]

        if not exact.empty:
            row = exact.iloc[0]
            return {
                "Type": row.get("Type"),
                "Family": row.get("Family"),
                "Group": row.get("Group"),
                "Products/Case": row.get("Products/Case"),
                "Units/Product": row.get("Unit"),
                "Unit Size": row.get("Unit2"),
                "Unit Measure": row.get("Unit Measure"),
            }

    # Step 3: fallback to group mode
    group_rows = top[top["Group"] == group]

    return {
        "Type": safe_mode(top, "Type"),
        "Family": safe_mode(top, "Family"),
        "Group": group,
        "Products/Case": safe_mode(group_rows, "Products/Case"),
        "Units/Product": safe_mode(group_rows, "Unit"),
        "Unit Size": safe_mode(group_rows, "Unit2"),
        "Unit Measure": safe_mode(group_rows, "Unit Measure"),
    }

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

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # CLEAN
        status.text("Cleaning data...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])

        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        # EXACT MATCH
        status.text("Matching UPCs...")
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)
        merged["All Retail UIDs"] = merged[product_uid]
        merged["All Families"] = merged[product_family]

        progress.progress(40)

        # FUZZY MATCH
        status.text("Fuzzy matching...")

        def fuzzy_match(row):
            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

            upc10 = row["m_10"]
            desc = row["desc_clean"]

            candidates = product_df[
                product_df["p_12"].astype(str).str.contains(upc10, na=False)
            ]

            if candidates.empty:
                return None, None, 0, "No Match"

            candidates = candidates.copy()
            candidates["score"] = candidates["desc_clean"].apply(
                lambda x: fuzz.partial_ratio(desc, x)
            )

            filtered = candidates[candidates["score"] >= 70]

            if filtered.empty:
                return None, None, 0, "No Match"

            return (
                list(set(filtered[product_uid])),
                list(set(filtered[product_family])),
                filtered["score"].max(),
                "10-digit Fuzzy Match"
            )

        results = merged.apply(fuzzy_match, axis=1)

        merged["All Retail UIDs"] = results.apply(lambda x: x[0])
        merged["All Families"] = results.apply(lambda x: x[1])
        merged["Match Score"] = results.apply(lambda x: x[2])
        merged["Match Type"] = results.apply(lambda x: x[3])

        progress.progress(70)

        # STORE VALIDATION
        status.text("Validating store-family...")

        merged["Retail UID"] = merged["All Retail UIDs"].apply(
            lambda x: x[0] if isinstance(x, list) else None
        )

        merged["Family"] = merged["All Families"].apply(
            lambda x: x[0] if isinstance(x, list) else None
        )

        merged["store_family_key"] = (
            merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
        )

        sf_df["store_family_key"] = (
            sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)
        )

        valid_keys = set(sf_df["store_family_key"])
        merged["Valid Store-Family"] = merged["store_family_key"].isin(valid_keys)

        progress.progress(85)

        # OUTPUTS
        status.text("Building outputs...")

        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates(subset=[main_upc])

        unmatched_df.columns = ["UPC", "Description"]

        # ENRICH
        cols = [
            "Type", "Family", "Group",
            "Products/Case", "Units/Product",
            "Unit Size", "Unit Measure"
        ]

        for col in cols:
            unmatched_df[col] = None

        for i, row in unmatched_df.iterrows():
            attrs = infer_attributes_full(
                row["Description"],
                product_df,
                product_desc
            )

            for key, val in attrs.items():
                unmatched_df.at[i, key] = val

        # TEMPLATE
        product_template = pd.DataFrame()

        product_template["ProductId"] = unmatched_df["UPC"]
        product_template["Product Name"] = unmatched_df["Description"]

        product_template["Type"] = unmatched_df["Type"]
        product_template["Family"] = unmatched_df["Family"]
        product_template["Group"] = unmatched_df["Group"]

        product_template["ProductUPC"] = unmatched_df["UPC"]
        product_template["UnitUPC"] = unmatched_df["UPC"]
        product_template["CaseUPC"] = unmatched_df["UPC"]

        product_template["Active"] = "true"

        product_template["Products/Case"] = unmatched_df["Products/Case"]
        product_template["Units/Product"] = unmatched_df["Units/Product"]
        product_template["Unit Size"] = unmatched_df["Unit Size"]
        product_template["Unit Measure"] = unmatched_df["Unit Measure"]

        product_template["Family Head"] = "false"

        # EXPORT
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)
            product_template.to_excel(writer, sheet_name="Product Template", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("Done!")

        st.download_button(
            "📥 Download File",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
