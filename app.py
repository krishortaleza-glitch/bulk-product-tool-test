import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
from rapidfuzz import fuzz, process
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
# PARSE PACK (GROUP + SIZE)
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()

    group, size, unit = None, None, None

    # 6/12, 24/200ml
    match = re.search(r"(\d+)\s*/\s*(\d+)\s*(oz|ml)?", desc)
    if match:
        group = f"{match.group(1)}pk"
        size = int(match.group(2))
        if match.group(3):
            unit = match.group(3).upper()

    # 12pk 12oz
    if not group:
        match = re.search(r"(\d+)\s*pk.*?(\d+)\s*(oz|ml)", desc)
        if match:
            group = f"{match.group(1)}pk"
            size = int(match.group(2))
            unit = match.group(3).upper()

    # fallback size
    if not size:
        match = re.search(r"(\d+)\s*(oz|ml)", desc)
        if match:
            size = int(match.group(1))
            unit = match.group(2).upper()

    # fallback group
    if not group:
        match = re.search(r"(\d+)\s*pk", desc)
        if match:
            group = f"{match.group(1)}pk"

    return group, size, unit

# ==============================
# BUILD MULTI-WORD BRAND LIST
# ==============================
def extract_brand_phrase(desc):
    words = str(desc).lower().split()
    return " ".join(words[:3])

# ==============================
# BRAND DETECTION
# ==============================
def detect_brand(desc, brand_list):
    desc_clean = str(desc).lower()

    sorted_brands = sorted(brand_list, key=lambda x: -len(x))

    for brand in sorted_brands:
        if brand in desc_clean:
            return brand

    match = process.extractOne(desc_clean, brand_list, scorer=fuzz.partial_ratio)
    if match and match[1] >= 85:
        return match[0]

    return None

# ==============================
# INFERENCE FUNCTION
# ==============================
def infer_attributes_full(desc, product_df, product_desc, group_size_map, brand_list):
    desc_clean = str(desc).lower()

    group, size, unit = parse_pack(desc_clean)
    brand = detect_brand(desc_clean, brand_list)

    # GROUP + SIZE match
    if group and size:
        config = group_size_map.get((group, size))
        if config:
            return {
                "Type": None,
                "Family": None,
                "Group": group,
                "Products/Case": config.get("Products/Case"),
                "Units/Product": config.get("Unit"),
                "Unit Size": size,
                "Unit Measure": config.get("Unit Measure") or unit,
            }

    # fallback group only
    group_rows = product_df[product_df["Group"] == group]

    def safe_mode(df, col):
        return df[col].mode().iloc[0] if col in df and not df[col].mode().empty else None

    # fuzzy for type/family
    if brand:
        candidates = product_df[
            product_df[product_desc].astype(str).str.lower().str.contains(brand, na=False)
        ].copy()

        if not candidates.empty:
            candidates["score"] = candidates[product_desc].apply(
                lambda x: fuzz.partial_ratio(desc_clean, str(x).lower())
            )
            top = candidates[candidates["score"] >= 75]

            type_val = safe_mode(top, "Type")
            family_val = safe_mode(top, "Family")
        else:
            type_val, family_val = None, None
    else:
        type_val, family_val = None, None

    return {
        "Type": type_val,
        "Family": family_val,
        "Group": group,
        "Products/Case": safe_mode(group_rows, "Products/Case"),
        "Units/Product": safe_mode(group_rows, "Unit"),
        "Unit Size": size or safe_mode(group_rows, "Unit2"),
        "Unit Measure": unit or safe_mode(group_rows, "Unit Measure"),
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

    # COLUMN SELECTORS
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

        # CLEAN
        main_df["desc_clean"] = clean_desc(main_df[main_desc])
        product_df["desc_clean"] = clean_desc(product_df[product_desc])

        generate_keys(main_df, main_upc, "m")

        product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
        product_df = product_df.explode("UPC_list")
        generate_keys(product_df, "UPC_list", "p")

        # BRAND LIST
        product_df["brand_phrase"] = product_df[product_desc].apply(extract_brand_phrase)
        brand_list = product_df["brand_phrase"].dropna().value_counts().head(300).index.tolist()

        # GROUP+SIZE MAP
        group_size_map = product_df.groupby(["Group", "Unit2"]).agg({
            "Products/Case": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
            "Unit": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
            "Unit Measure": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
        }).to_dict("index")

        # EXACT MATCH
        map_12 = product_df.groupby("p_12").agg({
            product_uid: lambda x: list(set(x)),
            product_family: lambda x: list(set(x))
        })

        merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)
        merged["All Retail UIDs"] = merged[product_uid]
        merged["All Families"] = merged[product_family]

        # FUZZY MATCH
        def fuzzy_match(row):
            if isinstance(row["All Retail UIDs"], list):
                return row["All Retail UIDs"], row["All Families"], 100, "UPC Match"

            return None, None, 0, "No Match"

        results = merged.apply(fuzzy_match, axis=1)

        merged["All Retail UIDs"] = results.apply(lambda x: x[0])
        merged["All Families"] = results.apply(lambda x: x[1])
        merged["Match Score"] = results.apply(lambda x: x[2])
        merged["Match Type"] = results.apply(lambda x: x[3])

        # STORE VALIDATION
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

        # REASON TAGGING
        def get_reason(row):
            if pd.isna(row["Retail UID"]) and not row["Valid Store-Family"]:
                return "No Match + Invalid Store-Family"
            elif pd.isna(row["Retail UID"]):
                return "No Match"
            elif not row["Valid Store-Family"]:
                return "Invalid Store-Family"
            return None

        merged["Reason Detail"] = merged.apply(get_reason, axis=1)

        # OUTPUTS
        good_df = merged[
            (merged["Retail UID"].notna()) &
            (merged["Valid Store-Family"])
        ]

        invalid_df = merged[
            (merged["Retail UID"].isna()) |
            (~merged["Valid Store-Family"])
        ][[main_store, main_upc, main_desc, "Reason Detail"]]

        invalid_df.columns = ["Store", "UPC", "Description", "Reason"]

        invalid_sf_df = merged[~merged["Valid Store-Family"]][
            [main_store, "Family"]
        ].drop_duplicates()

        # UNMATCHED
        unmatched_df = merged[merged["Match Type"] == "No Match"][
            [main_upc, main_desc]
        ].drop_duplicates(subset=[main_upc])

        unmatched_df.columns = ["UPC", "Description"]

        # ENRICH
        cols = ["Type", "Family", "Group", "Products/Case", "Units/Product", "Unit Size", "Unit Measure"]
        for col in cols:
            unmatched_df[col] = None

        for i, row in unmatched_df.iterrows():
            attrs = infer_attributes_full(
                row["Description"],
                product_df,
                product_desc,
                group_size_map,
                brand_list
            )
            for k, v in attrs.items():
                unmatched_df.at[i, k] = v

        # TEMPLATE
        product_template = pd.DataFrame({
            "ProductId": unmatched_df["UPC"],
            "Product Name": unmatched_df["Description"],
            "Type": unmatched_df["Type"],
            "Family": unmatched_df["Family"],
            "Group": unmatched_df["Group"],
            "ProductUPC": unmatched_df["UPC"],
            "UnitUPC": unmatched_df["UPC"],
            "CaseUPC": unmatched_df["UPC"],
            "Active": "true",
            "Products/Case": unmatched_df["Products/Case"],
            "Units/Product": unmatched_df["Units/Product"],
            "Unit Size": unmatched_df["Unit Size"],
            "Unit Measure": unmatched_df["Unit Measure"],
            "Family Head": "false"
        })

        # EXPORT
        output = BytesIO()
        with pd.ExcelWriter(output, engine="openpyxl") as writer:
            merged.to_excel(writer, sheet_name="Full Output", index=False)
            good_df.to_excel(writer, sheet_name="Good To Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid For Portal", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)
            product_template.to_excel(writer, sheet_name="Product Template", index=False)

        output.seek(0)

        st.download_button(
            "📥 Download File",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
