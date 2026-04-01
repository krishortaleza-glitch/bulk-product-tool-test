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
# PARSE PACK
# ==============================
def parse_pack(desc):
    desc = str(desc).lower()
    group, size, unit = None, None, None

    match = re.search(r"(\d+)\s*/\s*(\d+)\s*(oz|ml)?", desc)
    if match:
        group = f"{match.group(1)}pk"
        size = int(match.group(2))
        if match.group(3):
            unit = match.group(3).upper()

    if not group:
        match = re.search(r"(\d+)\s*pk.*?(\d+)\s*(oz|ml)", desc)
        if match:
            group = f"{match.group(1)}pk"
            size = int(match.group(2))
            unit = match.group(3).upper()

    if not size:
        match = re.search(r"(\d+)\s*(oz|ml)", desc)
        if match:
            size = int(match.group(1))
            unit = match.group(2).upper()

    if not group:
        match = re.search(r"(\d+)\s*pk", desc)
        if match:
            group = f"{match.group(1)}pk"

    return group, size, unit

# ==============================
# BRAND DETECTION
# ==============================
def extract_brand_phrase(desc):
    words = str(desc).lower().split()
    return " ".join(words[:3])

def detect_brand(desc, brand_list):
    desc = str(desc).lower()
    for b in sorted(brand_list, key=len, reverse=True):
        if b in desc:
            return b
    match = process.extractOne(desc, brand_list, scorer=fuzz.partial_ratio)
    return match[0] if match and match[1] >= 85 else None

# ==============================
# INFERENCE
# ==============================
def infer_attributes(desc, product_df, product_desc, group_size_map, brand_list):
    desc_clean = str(desc).lower()
    group, size, unit = parse_pack(desc_clean)
    brand = detect_brand(desc_clean, brand_list)

    # GROUP + SIZE MATCH
    if group and size:
        config = group_size_map.get((group, size))
        if config:
            return {
                "Type": None,
                "Family": None,
                "Group": group,
                "Products/Case": config.get("Products/Case"),
                "Units/Product": config.get("Units/Product"),
                "Unit Size": size,
                "Unit Measure": config.get("Unit Measure") or unit,
            }

    # FALLBACK
    group_rows = product_df[product_df["Group"] == group]

    def safe_mode(df, col):
        return df[col].mode().iloc[0] if col in df and not df[col].mode().empty else None

    # Type + Family from fuzzy
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
        "Units/Product": safe_mode(group_rows, "Units/Product"),
        "Unit Size": size or safe_mode(group_rows, "Unit Size"),
        "Unit Measure": unit or safe_mode(group_rows, "Unit Measure"),
    }

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

    # Build maps
    product_df["brand"] = product_df["Product Name"].apply(extract_brand_phrase)
    brand_list = product_df["brand"].dropna().value_counts().head(300).index.tolist()

    group_size_map = product_df.groupby(["Group", "Unit Size"]).agg({
        "Products/Case": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
        "Units/Product": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
        "Unit Measure": lambda x: x.mode().iloc[0] if not x.mode().empty else None,
    }).to_dict("index")

    # SIMPLE MATCH (no fuzzy UPC now)
    main_df["desc_clean"] = clean_desc(main_df.iloc[:,1])
    product_df["desc_clean"] = clean_desc(product_df["Product Name"])

    merged = main_df.copy()
    merged["Retail UID"] = None
    merged["Family"] = None
    merged["Match Type"] = "No Match"

    # STORE VALIDATION
    merged["store_family_key"] = merged.iloc[:,2].astype(str) + "|" + merged["Family"].astype(str)
    sf_df["store_family_key"] = sf_df.iloc[:,0].astype(str) + "|" + sf_df.iloc[:,1].astype(str)

    valid_keys = set(sf_df["store_family_key"])
    merged["Valid Store-Family"] = merged["store_family_key"].isin(valid_keys)

    # REASON
    def get_reason(row):
        if pd.isna(row["Retail UID"]) and not row["Valid Store-Family"]:
            return "No Match + Invalid Store-Family"
        elif pd.isna(row["Retail UID"]):
            return "No Match"
        elif not row["Valid Store-Family"]:
            return "Invalid Store-Family"
        return None

    merged["Reason"] = merged.apply(get_reason, axis=1)

    # UNMATCHED
    unmatched_df = merged[[main_df.columns[0], main_df.columns[1]]].drop_duplicates()
    unmatched_df.columns = ["UPC", "Description"]

    cols = ["Type","Family","Group","Products/Case","Units/Product","Unit Size","Unit Measure"]
    for c in cols:
        unmatched_df[c] = None

    for i, row in unmatched_df.iterrows():
        attrs = infer_attributes(
            row["Description"],
            product_df,
            "Product Name",
            group_size_map,
            brand_list
        )
        for k,v in attrs.items():
            unmatched_df.at[i,k] = v

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
        merged.to_excel(writer, "Full Output", index=False)
        unmatched_df.to_excel(writer, "Unmatched", index=False)
        product_template.to_excel(writer, "Product Template", index=False)

    st.download_button("Download", data=output.getvalue(), file_name="output.xlsx")
