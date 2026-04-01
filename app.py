import streamlit as st
import pandas as pd
from datetime import datetime
from io import BytesIO
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
adm_file = st.file_uploader("ADM File", type=["xlsx"])
product_file = st.file_uploader("Product File", type=["xlsx"])
store_file = st.file_uploader("Store File", type=["xlsx"])

if adm_file and product_file and store_file:

    st.success("✅ Files uploaded")

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    product_df.columns = product_df.columns.str.strip()

    st.subheader("Column Mapping")

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

    # 🚀 BUTTON
    if st.button("🚀 Process Files"):

        st.info("⚙️ Processing started...")

        try:
            # ==============================
            # CLEAN
            # ==============================
            main_df["desc_clean"] = clean_desc(main_df[main_desc])
            product_df["desc_clean"] = clean_desc(product_df[product_desc])

            generate_keys(main_df, main_upc, "m")

            product_df["UPC_list"] = product_df[[product_upc1, product_upc2]].values.tolist()
            product_df = product_df.explode("UPC_list")
            generate_keys(product_df, "UPC_list", "p")

            st.write("✅ Cleaning done")

            # ==============================
            # MATCH
            # ==============================
            map_12 = product_df.groupby("p_12").agg({
                product_uid: lambda x: list(set(x)),
                product_family: lambda x: list(set(x))
            })

            merged = main_df.merge(map_12, how="left", left_on="m_12", right_index=True)

            merged["Retail UID"] = merged[product_uid].apply(
                lambda x: x[0] if isinstance(x, list) else None
            )

            merged["Match Type"] = merged["Retail UID"].apply(
                lambda x: "UPC Match" if pd.notna(x) else "No Match"
            )

            st.write("✅ Matching done")

            # ==============================
            # UNMATCHED
            # ==============================
            unmatched_df = merged[merged["Match Type"] == "No Match"][
                [main_upc, main_desc]
            ].drop_duplicates()

            unmatched_df.columns = ["UPC", "Description"]

            st.write(f"🔍 Unmatched count: {len(unmatched_df)}")

            # ==============================
            # PARSE
            # ==============================
            pack = unmatched_df["Description"].apply(parse_pack)
            unmatched_df["Group"] = pack.apply(lambda x: x[0])
            unmatched_df["Unit Size"] = pack.apply(lambda x: x[1])
            unmatched_df["Unit Measure"] = pack.apply(lambda x: x[2])

            # ==============================
            # TEMPLATE
            # ==============================
            template_df = pd.DataFrame({
                "ProductId": unmatched_df["UPC"],
                "Product Name": unmatched_df["Description"],
                "Group": unmatched_df["Group"],
                "ProductUPC": unmatched_df["UPC"],
                "Active": "true",
                "Unit Size": unmatched_df["Unit Size"],
                "Unit Measure": unmatched_df["Unit Measure"],
                "Family Head": "false"
            })

            st.write("✅ Template built")

            # ==============================
            # EXPORT
            # ==============================
            output = BytesIO()
            writer = pd.ExcelWriter(output, engine="openpyxl")

            pd.DataFrame({"Status": ["OK"]}).to_excel(writer, sheet_name="Status", index=False)

            merged.to_excel(writer, sheet_name="Full Output", index=False)
            unmatched_df.to_excel(writer, sheet_name="Unmatched", index=False)
            template_df.to_excel(writer, sheet_name="Product Template", index=False)

            writer.close()
            output.seek(0)

            st.success("✅ Processing complete")

            st.download_button(
                "📥 Download File",
                data=output,
                file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
            )

        except Exception as e:
            st.error(f"❌ Error occurred: {e}")
