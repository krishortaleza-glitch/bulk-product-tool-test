import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz

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

# ==============================
# 🔥 IMPROVED FAMILY INFERENCE
# ==============================
def normalize_text(text):
    return (
        str(text)
        .lower()
        .replace("/", " ")
        .replace("-", " ")
        .strip()
    )

def extract_family(desc, product_df, family_col):
    desc_clean = normalize_text(desc)

    families = product_df[family_col].dropna().unique()

    best_match = ""
    best_score = 0

    desc_words = set(desc_clean.split())

    for fam in families:
        fam_clean = normalize_text(fam)
        fam_words = set(fam_clean.split())

        # Guardrail: require at least 1 overlapping word
        if len(desc_words.intersection(fam_words)) == 0:
            continue

        score = fuzz.token_set_ratio(desc_clean, fam_clean)

        # Boost if full phrase appears
        if fam_clean in desc_clean:
            score += 10

        if score > best_score:
            best_score = score
            best_match = fam

    # Threshold (tune if needed)
    if best_score >= 85:
        return best_match

    return ""

def extract_type(family, product_df):
    if not family or "Type" not in product_df.columns:
        return ""

    candidates = product_df[
        product_df["Family"].str.lower() == str(family).lower()
    ]

    if candidates.empty:
        return ""

    return candidates["Type"].mode().iloc[0]

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
    # STATIC COLUMN DEFINITIONS
    # ==============================
    product_upc1 = "ProductUPC"
    product_upc2 = "UnitUPC"
    product_desc = "Product Name"
    product_uid = "ProductId"
    product_family = "Family"

    sf_store = "Store"
    sf_family = "Family"

    # ==============================
    # VALIDATION
    # ==============================
    required_product_cols = [
        "ProductUPC", "UnitUPC", "Product Name", "ProductId", "Family"
    ]
    required_sf_cols = ["Store", "Family"]

    missing_product = [c for c in required_product_cols if c not in product_df.columns]
    missing_sf = [c for c in required_sf_cols if c not in sf_df.columns]

    if missing_product:
        st.error(f"❌ Product file missing columns: {missing_product}")
        st.stop()

    if missing_sf:
        st.error(f"❌ Store Family file missing columns: {missing_sf}")
        st.stop()

    # ==============================
    # ADM COLUMN SELECTION
    # ==============================
    st.header("Select ADM Columns")

    col1 = st.columns(1)[0]

    with col1:
        main_upc = st.selectbox("Main UPC", main_df.columns)
        main_desc = st.selectbox("Main Description", main_df.columns)
        main_store = st.selectbox("Main Store", main_df.columns)

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        # STEP 1 CLEAN
        status.text("🔄 Cleaning data...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])

        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        # STEP 2 UPC MATCH
        status.text("🔎 Matching exact UPCs...")
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)
        merged["All Retail UIDs"] = merged[product_uid]
        merged["All Families"] = merged[product_family]

        progress.progress(40)

        # STEP 3 MATCHING
        status.text("🧠 Running smart matching...")

        product_df["p_12_str"] = product_df["p_12"].astype(str)

        def fuzzy_match(row):

            # UPC match
            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

            desc = row["desc_clean"]
            upc10 = row["m_10"]

            # EXACT DESCRIPTION MATCH
            exact_desc_matches = product_df[
                product_df["desc_clean"] == desc
            ]

            if not exact_desc_matches.empty:
                return (
                    list(set(exact_desc_matches[product_uid])),
                    list(set(exact_desc_matches[product_family])),
                    100,
                    "Exact Description Match"
                )

            # FUZZY MATCH
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

            if all_uids:
                return list(set(all_uids)), list(set(all_families)), best_score, "10-digit Fuzzy Match"

            return None, None, 0, "No Match"

        results = merged.apply(fuzzy_match, axis=1)

        merged["All Retail UIDs"] = results.apply(lambda x: x[0])
        merged["All Families"] = results.apply(lambda x: x[1])
        merged["Match Score"] = results.apply(lambda x: x[2])
        merged["Match Type"] = results.apply(lambda x: x[3])

        progress.progress(70)

        # STEP 4 STORE FAMILY VALIDATION
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

        # STEP 5 OUTPUT
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
        ].drop_duplicates()
        unmatched_df.columns = ["UPC", "Description"]

        invalid_sf_df = merged[~merged["Valid Store-Family"]][
            [main_store, "Family"]
        ].drop_duplicates()
        invalid_sf_df.columns = ["Store", "Family"]

        summary = merged["Match Type"].value_counts().reset_index()
        summary.columns = ["Match Type", "Count"]

        # 🔥 IMPROVED INFERENCE APPLIED HERE
        unmatched_df["Family"] = unmatched_df["Description"].apply(
            lambda x: extract_family(x, product_df, product_family)
        )

        unmatched_df["Type"] = unmatched_df["Family"].apply(
            lambda x: extract_type(x, product_df)
        )

        # PRODUCT TEMPLATE
        template_df = pd.DataFrame({
            "ProductId": unmatched_df["UPC"],
            "UnitId": "",
            "CaseId": "",
            "Product Name": unmatched_df["Description"],
            "Type": unmatched_df["Type"],
            "Family": unmatched_df["Family"],
            "Group": "",
            "ProductUPC": unmatched_df["UPC"],
            "UnitUPC": "",
            "CaseUPC": "",
            "Active": "true",
            "Products/Case": "",
            "Units/Product": "",
            "Unit Size": "",
            "Unit Measure": "",
            "ParentId": "",
            "Family Head": "false"
        })

        # EXPORT
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            summary.to_excel(writer, sheet_name="Summary", index=False)
            good_df.to_excel(writer, sheet_name="Good To Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid For Portal", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched Products", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("✅ Done!")

        st.download_button(
            "📥 Download Processed File",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
