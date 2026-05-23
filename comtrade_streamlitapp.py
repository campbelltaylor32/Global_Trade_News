# Streamlit Commodity Trade Intelligence Dashboard

# ```python
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from sqlalchemy import create_engine, text
from datetime import datetime

# --------------------------------------------------
# PAGE CONFIG
# --------------------------------------------------
st.set_page_config(
    page_title="Commodity Trade Intelligence Dashboard",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --------------------------------------------------
# CUSTOM THEME
# --------------------------------------------------
st.markdown(
    """
    <style>
    .stApp {
        background-color: #081221;
        color: #FFFFFF;
    }

    section[data-testid="stSidebar"] {
        background-color: #0E1B2A;
    }

    .stMetric {
        background-color: #11243A;
        border-radius: 12px;
        padding: 10px;
        border: 1px solid #1E3A5F;
    }

    div[data-testid="metric-container"] {
        background-color: #11243A;
        border: 1px solid #274C77;
        padding: 12px;
        border-radius: 12px;
    }

    h1, h2, h3 {
        color: #CFE8FF;
    }
    </style>
    """,
    unsafe_allow_html=True
)

# --------------------------------------------------
# DATABASE CONNECTION
# --------------------------------------------------
# Replace with your credentials
DB_USER = "class_user"
DB_PASSWORD = "CLOU_!%40%23"
DB_HOST = "35.254.120.209"
DB_PORT = "3306"
DB_NAME = "comtrade"

DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
)

engine = create_engine(DATABASE_URL)

# --------------------------------------------------
# HELPER FUNCTIONS
# --------------------------------------------------
@st.cache_data(ttl=3600)
def load_trade_data():
    query = """
    SELECT
        ftg.reporter_desc,
        ftg.partner_desc,
        ftg.cmd_desc,
        ftg.primary_value_usd,
        ftg.qty,
        ftg.ref_year,
        ftg.ref_month,
        ftg.flow_desc,
        ftg.net_weight,
        tm.mot_desc,
        ce.event_label,
        ce.event_code,
        ce.event_date,
        ce.avg_tone,
        ce.goldstein_scale,
        ce.num_mentions
    FROM fact_trade_granular ftg
    LEFT JOIN transport_mapping tm
        ON ftg.mot_code = tm.mot_code
    LEFT JOIN commodity_events ce
        ON YEAR(ce.event_date) = ftg.ref_year
    LIMIT 100000
    """
    df = pd.read_sql(query, engine)
    df['year_month'] = pd.to_datetime(
        df['ref_year'].astype(str) + '-' + df['ref_month'].astype(str).str.zfill(2) + '-01'
    )
    return df


@st.cache_data(ttl=3600)
def load_monthly_trade():
    query = """
    SELECT
        ref_year,
        ref_month,
        flow_desc,
        cmd_desc,
        reporter_desc,
        SUM(primary_value_usd) AS total_value,
        SUM(qty) AS total_qty,
        COUNT(*) AS num_records
    FROM fact_trade_granular
    GROUP BY ref_year, ref_month, flow_desc, cmd_desc, reporter_desc
    ORDER BY ref_year, ref_month
    """
    df = pd.read_sql(query, engine)
    df['year_month'] = pd.to_datetime(
        df['ref_year'].astype(str) + '-' + df['ref_month'].astype(str).str.zfill(2) + '-01'
    )
    return df


@st.cache_data(ttl=3600)
def load_articles():
    query = """
    SELECT
        commodity,
        sector,
        article_date,
        ym,
        sentiment,
        source,
        title,
        url
    FROM commodity_articles
    ORDER BY article_date DESC
    LIMIT 5000
    """
    return pd.read_sql(query, engine)


@st.cache_data(ttl=3600)
def load_event_summary():
    query = """
    SELECT
        ce.event_label,
        ce.event_code,
        ce.event_date,
        ce.avg_tone,
        ce.goldstein_scale,
        ce.num_mentions,
        ce.commodity,
        ce.actor1_country,
        ce.actor2_country,
        COUNT(*) AS impacted_records,
        AVG(ftg.primary_value_usd) AS avg_trade_value,
        SUM(ftg.primary_value_usd) AS total_trade_value
    FROM commodity_events ce
    JOIN fact_trade_granular ftg
        ON YEAR(ce.event_date) = ftg.ref_year
    GROUP BY ce.event_label, ce.event_code, ce.event_date,
             ce.avg_tone, ce.goldstein_scale, ce.num_mentions,
             ce.commodity, ce.actor1_country, ce.actor2_country
    ORDER BY total_trade_value DESC
    LIMIT 1000
    """
    return pd.read_sql(query, engine)


@st.cache_data(ttl=3600)
def run_custom_query(query_text):
    with engine.connect() as connection:
        result = connection.execute(text(query_text))
        df = pd.DataFrame(result.fetchall(), columns=result.keys())
    return df


# --------------------------------------------------
# LOAD DATA
# --------------------------------------------------
try:
    trade_df = load_trade_data()
    monthly_df = load_monthly_trade()
    event_summary_df = load_event_summary()
    articles_df = load_articles()
except Exception as e:
    st.error(f"Database connection failed: {e}")
    st.stop()

# --------------------------------------------------
# SIDEBAR FILTERS
# --------------------------------------------------
st.sidebar.title("Global Filters")

country_filter = st.sidebar.multiselect(
    "Reporter Country",
    options=sorted(trade_df['reporter_desc'].dropna().unique())
)

partner_filter = st.sidebar.multiselect(
    "Partner Country",
    options=sorted(trade_df['partner_desc'].dropna().unique())
)

commodity_filter = st.sidebar.multiselect(
    "Commodity",
    options=sorted(trade_df['cmd_desc'].dropna().unique())
)

flow_filter = st.sidebar.multiselect(
    "Trade Flow",
    options=sorted(trade_df['flow_desc'].dropna().unique())
)

transport_filter = st.sidebar.multiselect(
    "Transport Mode",
    options=sorted(trade_df['mot_desc'].dropna().unique())
)

year_min = int(trade_df['ref_year'].min())
year_max = int(trade_df['ref_year'].max())

if year_min == year_max:
    selected_years = (year_min, year_max)
    st.sidebar.write(f"Year: {year_min}")
else:
    selected_years = st.sidebar.slider(
        "Year Range", year_min, year_max, (year_min, year_max)
    )

# --------------------------------------------------
# FILTER DATAFRAME
# --------------------------------------------------
filtered_df = trade_df.copy()
filtered_df = filtered_df[
    (filtered_df['ref_year'] >= selected_years[0]) &
    (filtered_df['ref_year'] <= selected_years[1])
]

if country_filter:
    filtered_df = filtered_df[filtered_df['reporter_desc'].isin(country_filter)]
if partner_filter:
    filtered_df = filtered_df[filtered_df['partner_desc'].isin(partner_filter)]
if commodity_filter:
    filtered_df = filtered_df[filtered_df['cmd_desc'].isin(commodity_filter)]
if flow_filter:
    filtered_df = filtered_df[filtered_df['flow_desc'].isin(flow_filter)]
if transport_filter:
    filtered_df = filtered_df[filtered_df['mot_desc'].isin(transport_filter)]

filtered_monthly = monthly_df.copy()
filtered_monthly = filtered_monthly[
    (filtered_monthly['ref_year'] >= selected_years[0]) &
    (filtered_monthly['ref_year'] <= selected_years[1])
]
if country_filter:
    filtered_monthly = filtered_monthly[filtered_monthly['reporter_desc'].isin(country_filter)]
if commodity_filter:
    filtered_monthly = filtered_monthly[filtered_monthly['cmd_desc'].isin(commodity_filter)]
if flow_filter:
    filtered_monthly = filtered_monthly[filtered_monthly['flow_desc'].isin(flow_filter)]

# --------------------------------------------------
# HEADER
# --------------------------------------------------
st.title("Commodity Trade Intelligence Dashboard")
st.markdown("Analyze how geopolitical, economic, and logistics events impact global imports and exports.")

# --------------------------------------------------
# KPI SECTION
# --------------------------------------------------
total_trade_value = filtered_df['primary_value_usd'].sum()
total_qty = filtered_df['qty'].sum()
avg_trade_value = filtered_df['primary_value_usd'].mean()
num_events = filtered_df['event_label'].nunique()
num_partners = filtered_df['partner_desc'].nunique()
num_commodities = filtered_df['cmd_desc'].nunique()

col1, col2, col3, col4, col5, col6 = st.columns(6)
col1.metric("Total Trade Value", f"${total_trade_value/1e9:.2f}B")
col2.metric("Total Quantity", f"{total_qty/1e6:.1f}M")
col3.metric("Avg Trade Value", f"${avg_trade_value:,.0f}")
col4.metric("Associated Events", num_events)
col5.metric("Trading Partners", num_partners)
col6.metric("Commodities", num_commodities)

# --------------------------------------------------
# TABS
# --------------------------------------------------
tab1, tab2, tab3, tab4, tab5, tab6 = st.tabs([
    "Executive Overview",
    "Monthly Seasonality",
    "Trade Flow & Partners",
    "Event & Sentiment Analysis",
    "Country Risk Intelligence",
    "Custom SQL Query"
])

# --------------------------------------------------
# TAB 1 - EXECUTIVE OVERVIEW
# --------------------------------------------------
with tab1:
    st.header("Executive Overview")

    # Monthly trade trend
    monthly_trend = (
        filtered_monthly.groupby(['year_month', 'flow_desc'])['total_value']
        .sum()
        .reset_index()
    )

    fig_monthly = px.line(
        monthly_trend,
        x='year_month',
        y='total_value',
        color='flow_desc',
        markers=True,
        title='Monthly Trade Value by Flow Type',
        template='plotly_dark',
        labels={'total_value': 'Trade Value (USD)', 'year_month': 'Month', 'flow_desc': 'Flow'}
    )
    st.plotly_chart(fig_monthly, use_container_width=True)

    col_a, col_b = st.columns(2)

    with col_a:
        top_commodities = (
            filtered_df.groupby('cmd_desc')['primary_value_usd']
            .sum()
            .sort_values(ascending=False)
            .head(10)
            .reset_index()
        )
        fig_commodities = px.bar(
            top_commodities,
            x='primary_value_usd',
            y='cmd_desc',
            orientation='h',
            title='Top 10 Commodities by Trade Value',
            template='plotly_dark',
            labels={'primary_value_usd': 'Trade Value (USD)', 'cmd_desc': 'Commodity'}
        )
        fig_commodities.update_layout(yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig_commodities, use_container_width=True)

    with col_b:
        # Value vs Quantity scatter per commodity
        scatter_data = (
            filtered_df.groupby('cmd_desc')
            .agg(total_value=('primary_value_usd', 'sum'), total_qty=('qty', 'sum'))
            .reset_index()
            .dropna()
        )
        scatter_data = scatter_data[scatter_data['total_qty'] > 0]
        fig_scatter = px.scatter(
            scatter_data,
            x='total_qty',
            y='total_value',
            hover_name='cmd_desc',
            title='Trade Value vs Quantity by Commodity',
            template='plotly_dark',
            labels={'total_value': 'Total Value (USD)', 'total_qty': 'Total Quantity'},
            size='total_value',
            size_max=40
        )
        st.plotly_chart(fig_scatter, use_container_width=True)

# --------------------------------------------------
# TAB 2 - MONTHLY SEASONALITY
# --------------------------------------------------
with tab2:
    st.header("Monthly Seasonality Analysis")
    st.markdown("Understand how trade volumes shift month by month throughout the year.")

    # Monthly heatmap: month vs commodity
    month_commodity = (
        filtered_monthly.groupby(['ref_month', 'cmd_desc'])['total_value']
        .sum()
        .reset_index()
    )
    top_10_cmds = (
        month_commodity.groupby('cmd_desc')['total_value']
        .sum()
        .sort_values(ascending=False)
        .head(10)
        .index.tolist()
    )
    month_commodity = month_commodity[month_commodity['cmd_desc'].isin(top_10_cmds)]

    pivot_season = month_commodity.pivot(
        index='cmd_desc', columns='ref_month', values='total_value'
    ).fillna(0)

    month_labels = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec']
    pivot_season.columns = [month_labels[c-1] for c in pivot_season.columns]

    fig_season = go.Figure(data=go.Heatmap(
        z=pivot_season.values,
        x=pivot_season.columns.tolist(),
        y=pivot_season.index.tolist(),
        colorscale='Blues',
        text=[[f"${v/1e6:.1f}M" for v in row] for row in pivot_season.values],
        texttemplate="%{text}",
        showscale=True
    ))
    fig_season.update_layout(
        title='Seasonality Heatmap: Trade Value by Commodity & Month',
        template='plotly_dark',
        height=450
    )
    st.plotly_chart(fig_season, use_container_width=True)

    # Monthly bar: import vs export
    monthly_flow = (
        filtered_monthly.groupby(['ref_month', 'flow_desc'])['total_value']
        .sum()
        .reset_index()
    )
    monthly_flow['month_name'] = monthly_flow['ref_month'].apply(lambda x: month_labels[x-1])

    fig_monthly_flow = px.bar(
        monthly_flow,
        x='month_name',
        y='total_value',
        color='flow_desc',
        barmode='group',
        title='Monthly Import vs Export Comparison',
        template='plotly_dark',
        labels={'total_value': 'Trade Value (USD)', 'month_name': 'Month', 'flow_desc': 'Flow'},
        category_orders={'month_name': month_labels}
    )
    st.plotly_chart(fig_monthly_flow, use_container_width=True)

# --------------------------------------------------
# TAB 3 - TRADE FLOW & PARTNERS
# --------------------------------------------------
with tab3:
    st.header("Trade Flow & Partner Analysis")

    col_a, col_b = st.columns(2)

    with col_a:
        transport_trade = (
            filtered_df.groupby('mot_desc')['primary_value_usd']
            .sum()
            .sort_values(ascending=False)
            .reset_index()
        )
        fig_transport = px.pie(
            transport_trade,
            names='mot_desc',
            values='primary_value_usd',
            title='Trade by Transport Mode',
            template='plotly_dark',
            hole=0.4
        )
        st.plotly_chart(fig_transport, use_container_width=True)

    with col_b:
        flow_commodity = (
            filtered_df.groupby(['flow_desc', 'cmd_desc'])['primary_value_usd']
            .sum()
            .reset_index()
        )
        top_cmds = (
            flow_commodity.groupby('cmd_desc')['primary_value_usd']
            .sum()
            .sort_values(ascending=False)
            .head(8)
            .index.tolist()
        )
        flow_commodity = flow_commodity[flow_commodity['cmd_desc'].isin(top_cmds)]
        fig_flow_cmd = px.bar(
            flow_commodity,
            x='cmd_desc',
            y='primary_value_usd',
            color='flow_desc',
            barmode='group',
            title='Import vs Export by Top Commodity',
            template='plotly_dark',
            labels={'primary_value_usd': 'Value (USD)', 'cmd_desc': 'Commodity', 'flow_desc': 'Flow'}
        )
        fig_flow_cmd.update_xaxes(tickangle=30)
        st.plotly_chart(fig_flow_cmd, use_container_width=True)

    # Top partners
    partner_trade = (
        filtered_df.groupby(['partner_desc', 'flow_desc'])['primary_value_usd']
        .sum()
        .reset_index()
    )
    top_partners = (
        partner_trade.groupby('partner_desc')['primary_value_usd']
        .sum()
        .sort_values(ascending=False)
        .head(15)
        .index.tolist()
    )
    partner_trade = partner_trade[partner_trade['partner_desc'].isin(top_partners)]

    fig_partner = px.bar(
        partner_trade,
        x='partner_desc',
        y='primary_value_usd',
        color='flow_desc',
        barmode='stack',
        title='Top 15 Trading Partners (Import + Export)',
        template='plotly_dark',
        labels={'primary_value_usd': 'Trade Value (USD)', 'partner_desc': 'Partner', 'flow_desc': 'Flow'}
    )
    fig_partner.update_xaxes(tickangle=40)
    st.plotly_chart(fig_partner, use_container_width=True)

    # Reporter treemap
    reporter_commodity = (
        filtered_df.groupby(['reporter_desc', 'cmd_desc'])['primary_value_usd']
        .sum()
        .reset_index()
    )
    fig_treemap = px.treemap(
        reporter_commodity,
        path=['reporter_desc', 'cmd_desc'],
        values='primary_value_usd',
        title='Trade Value by Reporter Country and Commodity',
        template='plotly_dark'
    )
    st.plotly_chart(fig_treemap, use_container_width=True)

# --------------------------------------------------
# TAB 4 - EVENT & SENTIMENT ANALYSIS
# --------------------------------------------------
with tab4:
    st.header("Event & Sentiment Analysis")

    col_a, col_b = st.columns(2)

    with col_a:
        # Goldstein scale distribution
        if 'goldstein_scale' in event_summary_df.columns and event_summary_df['goldstein_scale'].notna().any():
            fig_goldstein = px.histogram(
                event_summary_df.dropna(subset=['goldstein_scale']),
                x='goldstein_scale',
                nbins=30,
                title='Event Sentiment Distribution (Goldstein Scale)',
                template='plotly_dark',
                labels={'goldstein_scale': 'Goldstein Scale (-10 = conflict, +10 = cooperative)'}
            )
            st.plotly_chart(fig_goldstein, use_container_width=True)

    with col_b:
        # Top events by trade value
        top_events = (
            event_summary_df.groupby('event_label')['total_trade_value']
            .sum()
            .sort_values(ascending=False)
            .head(10)
            .reset_index()
        )
        fig_top_events = px.bar(
            top_events,
            x='total_trade_value',
            y='event_label',
            orientation='h',
            title='Top Events by Associated Trade Value',
            template='plotly_dark',
            labels={'total_trade_value': 'Trade Value (USD)', 'event_label': 'Event'}
        )
        fig_top_events.update_layout(yaxis={'categoryorder': 'total ascending'})
        st.plotly_chart(fig_top_events, use_container_width=True)

    # Sentiment vs trade value scatter
    event_scatter = event_summary_df.dropna(subset=['avg_tone', 'total_trade_value'])
    if not event_scatter.empty:
        fig_sentiment = px.scatter(
            event_scatter,
            x='avg_tone',
            y='total_trade_value',
            hover_name='event_label',
            size='num_mentions',
            color='goldstein_scale',
            color_continuous_scale='RdYlGn',
            title='Event Tone vs Trade Value (size = num mentions)',
            template='plotly_dark',
            labels={
                'avg_tone': 'Average Tone (negative → positive)',
                'total_trade_value': 'Total Trade Value (USD)',
                'goldstein_scale': 'Goldstein Scale'
            }
        )
        st.plotly_chart(fig_sentiment, use_container_width=True)

    # News articles sentiment
    st.subheader("News Article Sentiment by Commodity")
    if not articles_df.empty and articles_df['sentiment'].notna().any():
        sentiment_by_commodity = (
            articles_df.groupby('commodity')['sentiment']
            .mean()
            .sort_values()
            .reset_index()
        )
        fig_art_sentiment = px.bar(
            sentiment_by_commodity,
            x='sentiment',
            y='commodity',
            orientation='h',
            title='Average News Sentiment by Commodity',
            template='plotly_dark',
            color='sentiment',
            color_continuous_scale='RdYlGn',
            labels={'sentiment': 'Avg Sentiment Score', 'commodity': 'Commodity'}
        )
        st.plotly_chart(fig_art_sentiment, use_container_width=True)

        st.subheader("Latest News Articles")
        article_display = articles_df[['article_date', 'commodity', 'sector', 'title', 'sentiment', 'source']].head(20)
        st.dataframe(article_display, use_container_width=True)
    else:
        st.info("No article sentiment data available.")

    st.subheader("Event Intelligence Table")
    st.dataframe(event_summary_df, use_container_width=True)

# --------------------------------------------------
# TAB 5 - COUNTRY RISK INTELLIGENCE
# --------------------------------------------------
with tab5:
    st.header("Country Risk Intelligence")

    country_heatmap = (
        filtered_df.groupby(['reporter_desc', 'flow_desc'])['primary_value_usd']
        .sum()
        .reset_index()
    )
    pivot_heatmap = country_heatmap.pivot(
        index='reporter_desc',
        columns='flow_desc',
        values='primary_value_usd'
    ).fillna(0)

    fig_heatmap = go.Figure(data=go.Heatmap(
        z=pivot_heatmap.values,
        x=pivot_heatmap.columns.tolist(),
        y=pivot_heatmap.index.tolist(),
        colorscale='Blues'
    ))
    fig_heatmap.update_layout(
        title='Import / Export Exposure Heatmap by Country',
        template='plotly_dark',
        height=600
    )
    st.plotly_chart(fig_heatmap, use_container_width=True)

    # Country concentration risk
    country_total = (
        filtered_df.groupby('reporter_desc')['primary_value_usd']
        .sum()
        .sort_values(ascending=False)
        .head(20)
        .reset_index()
    )
    grand_total = country_total['primary_value_usd'].sum()
    country_total['share_pct'] = (country_total['primary_value_usd'] / grand_total * 100).round(2)

    fig_concentration = px.bar(
        country_total,
        x='reporter_desc',
        y='share_pct',
        title='Country Concentration Risk (% of Total Trade)',
        template='plotly_dark',
        labels={'share_pct': '% of Total Trade', 'reporter_desc': 'Country'}
    )
    fig_concentration.update_xaxes(tickangle=40)
    st.plotly_chart(fig_concentration, use_container_width=True)

    # Partner dependency
    st.subheader("Partner Dependency Analysis")
    dependency = (
        filtered_df.groupby(['reporter_desc', 'partner_desc'])['primary_value_usd']
        .sum()
        .reset_index()
        .sort_values('primary_value_usd', ascending=False)
        .head(30)
    )
    fig_dependency = px.sunburst(
        dependency,
        path=['reporter_desc', 'partner_desc'],
        values='primary_value_usd',
        title='Reporter → Partner Trade Dependency',
        template='plotly_dark'
    )
    st.plotly_chart(fig_dependency, use_container_width=True)

# --------------------------------------------------
# TAB 6 - CUSTOM SQL QUERY
# --------------------------------------------------
with tab6:
    st.header("Custom SQL Query Console")
    st.markdown("Run custom business intelligence queries directly against the commodity trade warehouse.")

    default_query = """
SELECT
    ref_year,
    ref_month,
    flow_desc,
    SUM(primary_value_usd) AS total_trade_value
FROM fact_trade_granular
GROUP BY ref_year, ref_month, flow_desc
ORDER BY ref_year, ref_month
LIMIT 100
    """

    custom_query = st.text_area("SQL Query", value=default_query, height=250)

    if st.button("Run Query"):
        try:
            query_result = run_custom_query(custom_query)
            st.success(f"Query executed successfully — {len(query_result):,} rows returned")
            st.dataframe(query_result, use_container_width=True)

            if len(query_result.columns) >= 2:
                numeric_cols = query_result.select_dtypes(include='number').columns
                if len(numeric_cols) > 0:
                    chart = px.bar(
                        query_result,
                        x=query_result.columns[0],
                        y=numeric_cols[0],
                        template='plotly_dark',
                        title='Query Result Visualization'
                    )
                    st.plotly_chart(chart, use_container_width=True)

        except Exception as e:
            st.error(f"Query failed: {e}")

# --------------------------------------------------
# FOOTER
# --------------------------------------------------
st.markdown("---")
st.caption("Commodity Trade Intelligence Platform | Event-Driven Import & Export Analytics")