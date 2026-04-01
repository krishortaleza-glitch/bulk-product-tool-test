import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz
import time

st.set_page_config(page_title="Bulk Product Request Tool", layout="wide")
st.title("📦 Bulk Product Request Tool")

# ==============================
# CACHED FILE LOADING
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

    # ==============================
    # PROCESS BUTTON
    # ==============================
    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # STEP 1: CLEAN DATA
        status.text("🔄 Cleaning data...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])
        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        # STEP 2: EXACT MATCH
        status.text("🔎 Matching exact UPCs...")
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)
        merged["All Retail UIDs"] = merged[product_uid]
        merged["All Families"] = merged[product_family]

        progress.progress(40)

        # STEP 3: FUZZY MATCH
        status.text("🧠 Running smart matching...")
        product_df["p_12_str"] = product_df["p_12"].astype(str)

        def fuzzy_match(row):
            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

            upc10 = row["m_10"]
            desc = row["desc_clean"]

            candidates = product_df[
                product_df["p_12_str"].str.contains(upc10, na=False)
            ]

            best_score = 0
            all_uids, all_families = [], []

            for _, r in candidates.iterrows():
                score = fuzz.partial_ratio(desc, r["desc_clean"])
                if score >= 70:
                    all_uids.append(r[product_uid])
                    all_families.append(r[product_family])
                    best_score = max(best_score, score)

            if not all_uids:
                return None, None, 0, "No Match"

            return list(set(all_uids)), list(set(all_families)), best_score, "10-digit Fuzzy Match"

        results = merged.apply(fuzzy_match, axis=1)

        merged["All Retail UIDs"] = results.apply(lambda x: x[0])
        merged["All Families"] = results.apply(lambda x: x[1])
        merged["Match Score"] = results.apply(lambda x: x[2])
        merged["Match Type"] = results.apply(lambda x: x[3])

        progress.progress(70)

        # STEP 4: STORE VALIDATION
        status.text("🏪 Validating store-family...")
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

        # STEP 5: OUTPUT
        status.text("📊 Building output...")

        good_df = merged[
            (merged["Retail UID"].notna()) &
            (merged["Valid Store-Family"])
        ][[main_store, "Retail UID"]].drop_duplicates()
        good_df.columns = ["Store", "Retail UID"]

        invalid_df = merged[
            (merged["Retail UID"].isna()) |
            (~merged["Valid Store-Family"])
        ][[main_store, main_upc, main_desc]]
        invalid_df.columns = ["Store", "UPC", "Description"]

        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ]
        unmatched_df.columns = ["UPC", "Description"]

        invalid_sf_df = merged[~merged["Valid Store-Family"]][
            [main_store, "Family"]
        ].drop_duplicates()
        invalid_sf_df.columns = ["Store", "Family"]

        summary = merged["Match Type"].value_counts().reset_index()
        summary.columns = ["Match Type", "Count"]

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            summary.to_excel(writer, sheet_name="Summary", index=False)
            good_df.to_excel(writer, sheet_name="Good To Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid For Portal", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched Products", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("✅ Done!")

        st.download_button(
            "📥 Download Processed File",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
