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
# FAMILY INFERENCE
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

    product_upc1 = "ProductUPC"
    product_upc2 = "UnitUPC"
    product_desc = "Product Name"
    product_uid = "ProductId"
    product_family = "Family"

    sf_store = "Store"
    sf_family = "Family"

    required_product_cols = [
        "ProductUPC", "UnitUPC", "Product Name", "ProductId", "Family"
    ]
    required_sf_cols = ["Store", "Family"]

    if any(c not in product_df.columns for c in required_product_cols):
        st.error("❌ Product file format incorrect")
        st.stop()

    if any(c not in sf_df.columns for c in required_sf_cols):
        st.error("❌ Store Family file format incorrect")
        st.stop()

    st.header("Select ADM Columns")

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

            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

            desc = row["desc_clean"]
            upc10 = row["m_10"]

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

        # FAMILY INFERENCE ONLY
        results = unmatched_df["Description"].apply(
            lambda x: infer_family_smart(x, product_df, product_desc, product_family)
        )

        unmatched_df["Family"] = results.apply(lambda x: x[0])
        unmatched_df["Type"] = results.apply(lambda x: x[1])

        # TEMPLATE
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
