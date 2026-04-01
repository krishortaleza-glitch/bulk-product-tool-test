import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz
import re
from collections import Counter

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

def normalize_text(text):
    return (
        str(text)
        .lower()
        .replace("/", " ")
        .replace("-", " ")
        .strip()
    )

def split_token_by_vocab(token, vocab):
    for i in range(1, len(token)):
        left = token[:i]
        right = token[i:]
        if left in vocab and right in vocab:
            return [left, right]
    return [token]

def clean_for_matching(text, product_vocab=None):
    text = normalize_text(text)
    text = re.sub(r"\b\d+/\d+\w*\b", "", text)
    text = re.sub(r"\b\d+\s?(pk|pack|ct)\b", "", text)
    text = re.sub(r"\b\d+(oz|ml|l)\b", "", text)
    text = text.replace("variety", "var")

    words = text.split()

    if product_vocab:
        new_words = []
        for w in words:
            if w not in product_vocab:
                new_words.extend(split_token_by_vocab(w, product_vocab))
            else:
                new_words.append(w)
        words = new_words

    return " ".join(words).strip()

def generate_keys(df, col, prefix):
    s = clean_upc(df[col])
    df[f"{prefix}_12"] = s.str.zfill(12)
    df[f"{prefix}_10"] = df[f"{prefix}_12"].str[-10:]

# ==============================
# FAMILY + TYPE INFERENCE
# ==============================
def infer_family_smart(desc, product_df, product_desc_col, family_col):

    product_vocab = set(
        " ".join(
            product_df[product_desc_col]
            .astype(str)
            .str.lower()
            .tolist()
        ).split()
    )

    desc_clean = clean_for_matching(desc, product_vocab)
    brand = desc_clean.split()[0] if desc_clean else ""

    filtered_products = product_df[
        product_df[product_desc_col]
        .astype(str)
        .str.lower()
        .str.contains(rf"\b{brand}\b", na=False)
    ]

    if filtered_products.empty:
        return "", ""

    scored = []

    for _, row in filtered_products.iterrows():
        prod_name = clean_for_matching(row[product_desc_col], product_vocab)

        score = max(
            fuzz.token_set_ratio(desc_clean, prod_name),
            fuzz.partial_ratio(desc_clean, prod_name)
        )

        if score >= 70:
            scored.append((score, row))

    if not scored:
        return "", ""

    top_matches = sorted(scored, key=lambda x: x[0], reverse=True)[:10]

    word_counter = Counter()
    for _, row in top_matches:
        words = clean_for_matching(row[product_desc_col], product_vocab).split()
        word_counter.update(set(words))

    common_words = [
        w for w, c in word_counter.items()
        if c >= len(top_matches) * 0.6
    ]

    base_phrase = " ".join(common_words)

    best_family = ""
    best_score = 0

    for _, row in top_matches:
        fam = row[family_col]
        fam_clean = clean_for_matching(fam, product_vocab)
        score = fuzz.token_set_ratio(base_phrase, fam_clean)

        if score > best_score:
            best_score = score
            best_family = fam

    best_type = ""
    if best_family:
        candidates = product_df[
            product_df[family_col].str.lower() == best_family.lower()
        ]
        if not candidates.empty and "Type" in product_df.columns:
            best_type = candidates["Type"].mode().iloc[0]

    return best_family, best_type

# ==============================
# 🔥 PACKAGE INFERENCE (FINAL FIX)
# ==============================
def infer_package_config(desc, family, product_df):

    text = str(desc).lower()

    units_per_product = None
    unit_size = None
    unit_measure = None
    products_per_case = None
    group = ""

    # STEP 1: parse packaging
    match = re.search(r"(\d+)\s*/\s*(\d+\.?\d*)\s*(oz|ml|l|c)?", text)
    if match:
        units_per_product = int(match.group(1))
        unit_size = float(match.group(2))
        unit_measure = match.group(3)

    if not units_per_product:
        match = re.search(r"(\d+)\s*(pk|pack|ct)", text)
        if match:
            units_per_product = int(match.group(1))

    if not unit_size:
        match = re.search(r"(\d+\.?\d*)\s*(oz|ml|l)", text)
        if match:
            unit_size = float(match.group(1))
            unit_measure = match.group(2)

    if unit_measure:
        unit_measure = unit_measure.upper()

    # STEP 2: filter same family
    fam_products = product_df[
        product_df["Family"].astype(str).str.lower() == str(family).lower()
    ].copy()

    if fam_products.empty:
        return "", 1, units_per_product, unit_size, unit_measure

    fam_products["clean_name"] = fam_products["Product Name"].astype(str).str.lower()

    # STEP 3: prioritize same units/product
    candidates = fam_products
    if units_per_product and "Units/Product" in fam_products.columns:
        subset = fam_products[fam_products["Units/Product"] == units_per_product]
        if not subset.empty:
            candidates = subset

    # STEP 4: best similarity match
    best_score = 0
    best_row = None

    for _, row in candidates.iterrows():
        score = fuzz.partial_ratio(text, row["clean_name"])
        if score > best_score:
            best_score = score
            best_row = row

    # STEP 5: apply match
    if best_row is not None:
        group = best_row.get("Group", "")
        products_per_case = best_row.get("Products/Case", 1)
        units_per_product = best_row.get("Units/Product", units_per_product)
        unit_size = best_row.get("Unit Size", unit_size)
        unit_measure = best_row.get("Unit Measure", unit_measure)

    # STEP 6: single rule
    if units_per_product == 1:
        unit_size = fam_products["Unit Size"].mode().iloc[0]
        unit_measure = fam_products["Unit Measure"].mode().iloc[0]
        products_per_case = 1

    # cleanup
    if not unit_measure and unit_size:
        unit_measure = "OZ" if unit_size <= 32 else "ML"

    if not products_per_case:
        products_per_case = 1

    return group, products_per_case, units_per_product, unit_size, unit_measure

# ==============================
# UI + PIPELINE (UNCHANGED)
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

    product_upc1 = "ProductUPC"
    product_upc2 = "UnitUPC"
    product_desc = "Product Name"
    product_uid = "ProductId"
    product_family = "Family"

    sf_store = "Store"
    sf_family = "Family"

    st.header("Select ADM Columns")

    main_upc = st.selectbox("Main UPC", main_df.columns)
    main_desc = st.selectbox("Main Description", main_df.columns)
    main_store = st.selectbox("Main Store", main_df.columns)

    if st.button("🚀 Process Files"):

        progress = st.progress(0)
        status = st.empty()

        status.text("🔄 Cleaning data...")
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])

        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        progress.progress(20)

        status.text("🔎 Matching exact UPCs...")
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)
        merged["All Retail UIDs"] = merged[product_uid]
        merged["All Families"] = merged[product_family]

        progress.progress(40)

        status.text("🧠 Running smart matching...")

        def fuzzy_match(row):
            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"
            return None, None, 0, "No Match"

        results = merged.apply(fuzzy_match, axis=1)

        merged["All Retail UIDs"] = results.apply(lambda x: x[0])
        merged["All Families"] = results.apply(lambda x: x[1])
        merged["Match Score"] = results.apply(lambda x: x[2])
        merged["Match Type"] = results.apply(lambda x: x[3])

        progress.progress(70)

        status.text("📊 Building output...")

        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates()

        unmatched_df.columns = ["UPC", "Description"]

        # FAMILY
        results = unmatched_df["Description"].apply(
            lambda x: infer_family_smart(x, product_df, product_desc, product_family)
        )

        unmatched_df["Family"] = results.apply(lambda x: x[0])
        unmatched_df["Type"] = results.apply(lambda x: x[1])

        # PACKAGE
        pkg = unmatched_df.apply(
            lambda row: infer_package_config(row["Description"], row["Family"], product_df),
            axis=1
        )

        unmatched_df["Group"] = pkg.apply(lambda x: x[0])
        unmatched_df["Products/Case"] = pkg.apply(lambda x: x[1])
        unmatched_df["Units/Product"] = pkg.apply(lambda x: x[2])
        unmatched_df["Unit Size"] = pkg.apply(lambda x: x[3])
        unmatched_df["Unit Measure"] = pkg.apply(lambda x: x[4])

        template_df = unmatched_df

        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched Products", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

        output.seek(0)

        progress.progress(100)
        status.text("✅ Done!")

        st.download_button(
            "📥 Download Processed File",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
