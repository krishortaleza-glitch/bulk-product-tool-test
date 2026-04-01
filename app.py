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

def safe_mode(df, col):
    if col not in df.columns or df.empty:
        return ""
    if df[col].mode().empty:
        return ""
    return df[col].mode().iloc[0]

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

    main_df = load_file(adm_file)
    product_df = load_file(product_file)
    sf_df = load_file(store_file)

    product_df.columns = product_df.columns.str.strip()

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

        output = BytesIO()

        # 🔒 ALWAYS CREATE FILE FIRST
        with pd.ExcelWriter(output, engine="openpyxl") as writer:

            # ✅ ALWAYS WRITE SAFE SHEET FIRST
            pd.DataFrame({"Status": ["Processing started"]}).to_excel(
                writer, "Status", index=False
            )

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
                # UNMATCHED
                # ==============================
                unmatched_df = merged[merged["Match Type"] == "No Match"][
                    [main_upc, main_desc]
                ].drop_duplicates()

                unmatched_df.columns = ["UPC", "Description"]

                # ==============================
                # PARSE PACK
                # ==============================
                pack = unmatched_df["Description"].apply(parse_pack)
                unmatched_df["Group"] = pack.apply(lambda x: x[0])
                unmatched_df["Unit Size"] = pack.apply(lambda x: x[1])
                unmatched_df["Unit Measure"] = pack.apply(lambda x: x[2])

                # ==============================
                # INFER CONFIG
                # ==============================
                def infer(row):
                    candidates = product_df.copy()

                    if "Group" in candidates.columns:
                        candidates = candidates[candidates["Group"] == row["Group"]]

                    return pd.Series({
                        "Products/Case": safe_mode(candidates, "Products/Case"),
                        "Units/Product": safe_mode(candidates, "Units/Product"),
                    })

                config = unmatched_df.apply(infer, axis=1)
                unmatched_df["Products/Case"] = config["Products/Case"]
                unmatched_df["Units/Product"] = config["Units/Product"]

                # ==============================
                # TEMPLATE
                # ==============================
                template_df = pd.DataFrame({
                    "ProductId": unmatched_df["UPC"],
                    "Product Name": unmatched_df["Description"],
                    "Group": unmatched_df["Group"],
                    "ProductUPC": unmatched_df["UPC"],
                    "Active": "true",
                    "Products/Case": unmatched_df["Products/Case"],
                    "Units/Product": unmatched_df["Units/Product"],
                    "Unit Size": unmatched_df["Unit Size"],
                    "Unit Measure": unmatched_df["Unit Measure"],
                    "Family Head": "false"
                })

                # ==============================
                # WRITE OUTPUTS
                # ==============================
                if not merged.empty:
                    merged.to_excel(writer, "Full Output", index=False)

                if not unmatched_df.empty:
                    unmatched_df.to_excel(writer, "Unmatched", index=False)

                if not template_df.empty:
                    template_df.to_excel(writer, "Product Template", index=False)

            except Exception as e:
                # 🔥 NEVER BREAK FILE
                pd.DataFrame({"Error": [str(e)]}).to_excel(
                    writer, "Error Log", index=False
                )
                st.error(f"Processing error: {e}")

        output.seek(0)

        st.download_button(
            "📥 Download",
            data=output,
            file_name=f"processed_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        )
