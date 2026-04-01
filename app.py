import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO

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

def safe_mode(df, col):
    if col not in df.columns or df.empty:
        return ""
    try:
        return df[col].mode().iloc[0]
    except:
        return ""

def extract_family(desc, product_df):
    desc = str(desc).lower()
    for fam in product_df["Family"].dropna().unique():
        if str(fam).lower() in desc:
            return fam
    return ""

def extract_type(family, product_df):
    if not family:
        return ""
    candidates = product_df[product_df["Family"] == family]
    return safe_mode(candidates, "Type")

# ==============================
# UI
# ==============================
adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store File", type=["xlsx"])

if adm_file and product_file and store_file:

    st.success("✅ Files uploaded")

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    product_df.columns = product_df.columns.str.strip()

    # COLUMN SELECTION
    main_upc = st.selectbox("Main UPC", main_df.columns)
    main_desc = st.selectbox("Main Description", main_df.columns)
    main_store = st.selectbox("Main Store", main_df.columns)

    product_upc1 = st.selectbox("Product UPC 1", product_df.columns)
    product_upc2 = st.selectbox("Product UPC 2", product_df.columns)
    product_desc = st.selectbox("Product Description", product_df.columns)
    product_uid = st.selectbox("Product UID", product_df.columns)
    product_family = st.selectbox("Product Family", product_df.columns)

    sf_store = st.selectbox("Store Column", sf_df.columns)
    sf_family = st.selectbox("Family Column", sf_df.columns)

    if st.button("🚀 Process Files"):

        try:
            st.write("Processing...")

            # CLEAN
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])

            generate_keys(main_df, main_upc, "m")

            product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
            product_df = product_df.explode("UPC_list")
            generate_keys(product_df, "UPC_list", "p")

            # ==============================
            # EXACT MATCH
            # ==============================
            map_12 = product_df.groupby("p_12").agg({
                product_uid: lambda x: list(set(x)),
                product_family: lambda x: list(set(x))
            })

            merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

            merged["Retail UID"] = merged[product_uid].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Family"] = merged[product_family].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Match Type"] = merged["Retail UID"].apply(
                lambda x: "UPC Match" if pd.notna(x) else "No Match"
            )

            # ==============================
            # STORE VALIDATION
            # ==============================
            merged["store_family_key"] = merged[main_store].astype(str) + "|" + merged["Family"].astype(str)
            sf_df["store_family_key"] = sf_df[sf_store].astype(str) + "|" + sf_df[sf_family].astype(str)

            merged["Valid Store-Family"] = merged["store_family_key"].isin(set(sf_df["store_family_key"]))

            # ==============================
            # OUTPUT TABS
            # ==============================
            good_df = merged[
                (merged["Retail UID"].notna()) &
                (merged["Valid Store-Family"])
            ]

            invalid_df = merged[
                (merged["Retail UID"].isna()) |
                (~merged["Valid Store-Family"])
            ]

            invalid_sf_df = merged[
                ~merged["Valid Store-Family"]
            ]

            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()

            unmatched_df.columns = ["UPC", "Description"]

            # ==============================
            # TYPE + FAMILY INFERENCE ONLY
            # ==============================
            unmatched_df["Family"] = unmatched_df["Description"].apply(
                lambda x: extract_family(x, product_df)
            )

            unmatched_df["Type"] = unmatched_df["Family"].apply(
                lambda x: extract_type(x, product_df)
            )

            # ==============================
            # PRODUCT TEMPLATE
            # ==============================
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

            # ==============================
            # EXPORT (ALL TABS RESTORED)
            # ==============================
            output = BytesIO()
            writer = pd.ExcelWriter(output, engine="openpyxl")

            merged.to_excel(writer, sheet_name="Full Output", index=False)
            good_df.to_excel(writer, sheet_name="Good to Go", index=False)
            invalid_df.to_excel(writer, sheet_name="Invalid for Portal", index=False)
            invalid_sf_df.to_excel(writer, sheet_name="Invalid Store Family", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

            writer.close()
            output.seek(0)

            st.success("✅ Done")

            st.download_button(
                "📥 Download",
                data=output,
                file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            )

        except Exception as e:
            st.error(f"❌ Error: {e}")
