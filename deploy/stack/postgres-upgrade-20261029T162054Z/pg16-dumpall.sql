--
-- PostgreSQL database cluster dump
--

\restrict rGeL0FiZWK1TohJck7fmEYXwelMrpCeZzUB4ZfXDaDh8CNYxXcBmuytYIbUkK46

SET default_transaction_read_only = off;

SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;

--
-- Roles
--

CREATE ROLE fdp;
ALTER ROLE fdp WITH SUPERUSER INHERIT CREATEROLE CREATEDB LOGIN REPLICATION BYPASSRLS PASSWORD 'SCRAM-SHA-256$4096:2nB8P/Jlcxohzc6722oO7w==$N97/WvCqjjTRqxH1pLnFFxIxdqznt4RX0rQFIiokvLI=:pwfktWe3PWdk1U6pVXrVAGsWxSe7Yuba2V3nUyUKCrE=';

--
-- User Configurations
--








\unrestrict rGeL0FiZWK1TohJck7fmEYXwelMrpCeZzUB4ZfXDaDh8CNYxXcBmuytYIbUkK46

--
-- Databases
--

--
-- Database "template1" dump
--

\connect template1

--
-- PostgreSQL database dump
--

\restrict rPi4Xz4TLWVARJY0CzSEgZn4mtkQeAZ1jnjWounUiP7kHRvpS37aAXYfw3E01po

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- PostgreSQL database dump complete
--

\unrestrict rPi4Xz4TLWVARJY0CzSEgZn4mtkQeAZ1jnjWounUiP7kHRvpS37aAXYfw3E01po

--
-- Database "fdp" dump
--

--
-- PostgreSQL database dump
--

\restrict 34MVcw68puyZbC1wVsTmIjOveC5rgAoTvRj7eCtWw42fPD5I6mcoWONmS84Weyo

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: fdp; Type: DATABASE; Schema: -; Owner: fdp
--

CREATE DATABASE fdp WITH TEMPLATE = template0 ENCODING = 'UTF8' LOCALE_PROVIDER = libc LOCALE = 'en_US.utf8';


ALTER DATABASE fdp OWNER TO fdp;

\unrestrict 34MVcw68puyZbC1wVsTmIjOveC5rgAoTvRj7eCtWw42fPD5I6mcoWONmS84Weyo
\connect fdp
\restrict 34MVcw68puyZbC1wVsTmIjOveC5rgAoTvRj7eCtWw42fPD5I6mcoWONmS84Weyo

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: alembic_version; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.alembic_version (
    version_num character varying(32) NOT NULL
);


ALTER TABLE public.alembic_version OWNER TO fdp;

--
-- Name: api_keys; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.api_keys (
    id character varying(36) NOT NULL,
    owner_subject character varying(2048) NOT NULL,
    label character varying(256) NOT NULL,
    key_hash character varying(64) NOT NULL,
    display_prefix character varying(32) NOT NULL,
    roles_json jsonb NOT NULL,
    groups_json jsonb NOT NULL,
    created_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone,
    last_used_at timestamp with time zone,
    revoked_at timestamp with time zone
);


ALTER TABLE public.api_keys OWNER TO fdp;

--
-- Name: authz_index; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.authz_index (
    id bigint NOT NULL,
    subject_key character varying(256) NOT NULL,
    action character varying(32) NOT NULL,
    graph_uri character varying(1024) NOT NULL,
    decision character varying(16) NOT NULL,
    policy_version character varying(1024),
    computed_at timestamp with time zone NOT NULL
);


ALTER TABLE public.authz_index OWNER TO fdp;

--
-- Name: authz_index_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.authz_index_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.authz_index_id_seq OWNER TO fdp;

--
-- Name: authz_index_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.authz_index_id_seq OWNED BY public.authz_index.id;


--
-- Name: index_targets; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.index_targets (
    id character varying(36) NOT NULL,
    url character varying(2048) NOT NULL,
    note character varying(512),
    created_at timestamp with time zone NOT NULL,
    created_by character varying(2048),
    last_ping_at timestamp with time zone,
    last_status_code integer,
    last_ok boolean,
    last_detail character varying(1024)
);


ALTER TABLE public.index_targets OWNER TO fdp;

--
-- Name: job_state; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.job_state (
    id bigint NOT NULL
);


ALTER TABLE public.job_state OWNER TO fdp;

--
-- Name: job_state_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.job_state_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.job_state_id_seq OWNER TO fdp;

--
-- Name: job_state_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.job_state_id_seq OWNED BY public.job_state.id;


--
-- Name: metadata_external_labels; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.metadata_external_labels (
    iri character varying(2048) NOT NULL,
    language character varying(32) NOT NULL,
    label text,
    resolved_at timestamp with time zone NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    source_host character varying(255)
);


ALTER TABLE public.metadata_external_labels OWNER TO fdp;

--
-- Name: metadata_search; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.metadata_search (
    record_iri character varying(2048) NOT NULL,
    type_iri character varying(2048),
    title text,
    description text,
    license character varying(2048),
    keywords text,
    search_text tsvector,
    state character varying(16),
    anon_read boolean DEFAULT false NOT NULL,
    updated_at timestamp with time zone,
    language character varying(32) DEFAULT 'english'::character varying NOT NULL
);


ALTER TABLE public.metadata_search OWNER TO fdp;

--
-- Name: metrics_daily; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.metrics_daily (
    id bigint NOT NULL,
    bucket date NOT NULL,
    event_type character varying(32) NOT NULL,
    resource_iri character varying(2048),
    country_code character varying(2),
    region character varying(128),
    city character varying(128),
    request_count bigint DEFAULT '0'::bigint NOT NULL,
    unique_visitors bigint DEFAULT '0'::bigint NOT NULL,
    latency_ms_sum bigint DEFAULT '0'::bigint NOT NULL,
    status_2xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_3xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_4xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_5xx_count bigint DEFAULT '0'::bigint NOT NULL
);


ALTER TABLE public.metrics_daily OWNER TO fdp;

--
-- Name: metrics_daily_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.metrics_daily_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.metrics_daily_id_seq OWNER TO fdp;

--
-- Name: metrics_daily_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.metrics_daily_id_seq OWNED BY public.metrics_daily.id;


--
-- Name: metrics_hourly; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.metrics_hourly (
    id bigint NOT NULL,
    bucket timestamp with time zone NOT NULL,
    event_type character varying(32) NOT NULL,
    resource_iri character varying(2048),
    country_code character varying(2),
    region character varying(128),
    city character varying(128),
    request_count bigint DEFAULT '0'::bigint NOT NULL,
    unique_visitors bigint DEFAULT '0'::bigint NOT NULL,
    latency_ms_sum bigint DEFAULT '0'::bigint NOT NULL,
    status_2xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_3xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_4xx_count bigint DEFAULT '0'::bigint NOT NULL,
    status_5xx_count bigint DEFAULT '0'::bigint NOT NULL
);


ALTER TABLE public.metrics_hourly OWNER TO fdp;

--
-- Name: metrics_hourly_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.metrics_hourly_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.metrics_hourly_id_seq OWNER TO fdp;

--
-- Name: metrics_hourly_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.metrics_hourly_id_seq OWNED BY public.metrics_hourly.id;


--
-- Name: metrics_raw; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.metrics_raw (
    id bigint NOT NULL,
    bucket timestamp with time zone NOT NULL,
    event_type character varying(32) NOT NULL,
    resource_iri character varying(2048),
    country_code character varying(2),
    region character varying(128),
    city character varying(128),
    visitor_hash character varying(32),
    status_code integer NOT NULL,
    latency_ms integer NOT NULL,
    recorded_at timestamp with time zone DEFAULT now() NOT NULL
);


ALTER TABLE public.metrics_raw OWNER TO fdp;

--
-- Name: metrics_raw_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.metrics_raw_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.metrics_raw_id_seq OWNER TO fdp;

--
-- Name: metrics_raw_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.metrics_raw_id_seq OWNED BY public.metrics_raw.id;


--
-- Name: policy_decisions_audit; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.policy_decisions_audit (
    id bigint NOT NULL
);


ALTER TABLE public.policy_decisions_audit OWNER TO fdp;

--
-- Name: policy_decisions_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.policy_decisions_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.policy_decisions_audit_id_seq OWNER TO fdp;

--
-- Name: policy_decisions_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.policy_decisions_audit_id_seq OWNED BY public.policy_decisions_audit.id;


--
-- Name: profile_applied; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.profile_applied (
    id bigint NOT NULL,
    name character varying(256) NOT NULL,
    version character varying(64) NOT NULL,
    applied_at timestamp with time zone NOT NULL,
    manifest_checksum character varying(64) NOT NULL
);


ALTER TABLE public.profile_applied OWNER TO fdp;

--
-- Name: profile_applied_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.profile_applied_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.profile_applied_id_seq OWNER TO fdp;

--
-- Name: profile_applied_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.profile_applied_id_seq OWNED BY public.profile_applied.id;


--
-- Name: record_audit; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.record_audit (
    id bigint NOT NULL,
    record_iri character varying(2048) NOT NULL,
    operation character varying(16) NOT NULL,
    subject character varying(2048),
    etag character varying(64),
    occurred_at timestamp with time zone NOT NULL
);


ALTER TABLE public.record_audit OWNER TO fdp;

--
-- Name: record_audit_id_seq; Type: SEQUENCE; Schema: public; Owner: fdp
--

CREATE SEQUENCE public.record_audit_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


ALTER SEQUENCE public.record_audit_id_seq OWNER TO fdp;

--
-- Name: record_audit_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: fdp
--

ALTER SEQUENCE public.record_audit_id_seq OWNED BY public.record_audit.id;


--
-- Name: runtime_settings; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.runtime_settings (
    key character varying(128) NOT NULL,
    value_json jsonb NOT NULL,
    updated_by character varying(2048),
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.runtime_settings OWNER TO fdp;

--
-- Name: search_saved_queries; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.search_saved_queries (
    id character varying(36) NOT NULL,
    owner_subject character varying(2048) NOT NULL,
    name character varying(256) NOT NULL,
    query_json jsonb NOT NULL,
    shared boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone NOT NULL
);


ALTER TABLE public.search_saved_queries OWNER TO fdp;

--
-- Name: subject_principal; Type: TABLE; Schema: public; Owner: fdp
--

CREATE TABLE public.subject_principal (
    subject character varying(2048) NOT NULL,
    roles_json jsonb NOT NULL,
    groups_json jsonb NOT NULL,
    updated_at timestamp with time zone NOT NULL
);


ALTER TABLE public.subject_principal OWNER TO fdp;

--
-- Name: authz_index id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.authz_index ALTER COLUMN id SET DEFAULT nextval('public.authz_index_id_seq'::regclass);


--
-- Name: job_state id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.job_state ALTER COLUMN id SET DEFAULT nextval('public.job_state_id_seq'::regclass);


--
-- Name: metrics_daily id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_daily ALTER COLUMN id SET DEFAULT nextval('public.metrics_daily_id_seq'::regclass);


--
-- Name: metrics_hourly id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_hourly ALTER COLUMN id SET DEFAULT nextval('public.metrics_hourly_id_seq'::regclass);


--
-- Name: metrics_raw id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_raw ALTER COLUMN id SET DEFAULT nextval('public.metrics_raw_id_seq'::regclass);


--
-- Name: policy_decisions_audit id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.policy_decisions_audit ALTER COLUMN id SET DEFAULT nextval('public.policy_decisions_audit_id_seq'::regclass);


--
-- Name: profile_applied id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.profile_applied ALTER COLUMN id SET DEFAULT nextval('public.profile_applied_id_seq'::regclass);


--
-- Name: record_audit id; Type: DEFAULT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.record_audit ALTER COLUMN id SET DEFAULT nextval('public.record_audit_id_seq'::regclass);


--
-- Data for Name: alembic_version; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.alembic_version (version_num) FROM stdin;
0010_index_targets
\.


--
-- Data for Name: api_keys; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.api_keys (id, owner_subject, label, key_hash, display_prefix, roles_json, groups_json, created_at, expires_at, last_used_at, revoked_at) FROM stdin;
f1554fb5-2159-4c7a-b398-90ebbeec10de	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	demo script	2b95b50c9538e4049da084ffa1a6c4aeb9a5cdf675feb043c220d3a351f29611	fdpk_Id5Klk3D…9YVY	["steward"]	[]	2026-10-28 11:58:28.773943+00	\N	2026-10-28 11:58:28.845923+00	2026-10-28 11:58:38.547521+00
25a4bac6-c632-45bb-bacc-7fd0e8d29ade	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	demo script	10283d77fd5db36bd79dd1fcae7c3287dc9a0de90b732710bf690eb078a6cada	fdpk_zTJfzvu6…ciyw	["admin", "steward"]	[]	2026-10-28 13:57:44.202493+00	\N	\N	2026-10-28 14:01:45.277408+00
\.


--
-- Data for Name: authz_index; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.authz_index (id, subject_key, action, graph_uri, decision, policy_version, computed_at) FROM stdin;
587	anonymous	read	http://localhost:8000	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.731864+00
621	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58#d6728bc894cc7064	read	http://localhost:8000	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:27.9695+00
623	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58#d6728bc894cc7064	read	http://localhost:8000/catalog/environmental-monitoring	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:30.180332+00
592	anonymous	read	http://localhost:8000/distribution/river-temperature-csv	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.760364+00
593	anonymous	read	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.767071+00
594	anonymous	read	http://localhost:8000/dataset/air-quality-enschede-2024	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.773234+00
595	anonymous	read	http://localhost:8000/fdp-api/schemas/data-service	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.778185+00
596	anonymous	read	http://localhost:8000/fdp-api/schemas/distribution	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.783529+00
597	anonymous	read	urn:fdp-shape:license-document	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.787351+00
598	anonymous	read	http://localhost:8000/catalog/clinical-research	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.793455+00
599	anonymous	read	http://localhost:8000/distribution/air-quality-2024-csv	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.799981+00
600	anonymous	read	http://localhost:8000/distribution/air-quality-2024-stations	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.806103+00
601	anonymous	read	http://localhost:8000/dataset/biobank-sample-catalog	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.812285+00
602	anonymous	read	http://localhost:8000/fdp-api/schemas/metadata-service	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.816276+00
603	anonymous	read	http://localhost:8000/fdp-api/schemas/dataset	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.820556+00
604	anonymous	read	http://localhost:8000/dataset/river-water-temperature	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.825607+00
605	anonymous	read	http://localhost:8000/fdp-api/schemas/fairdata-point	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.829046+00
606	anonymous	read	http://localhost:8000/distribution/biobank-access-request	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.837566+00
607	anonymous	read	urn:fdp-shape:resource-definition	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.8417+00
608	anonymous	read	http://localhost:8000/fdp-api/schemas/metadata	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.844908+00
609	anonymous	read	http://localhost:8000/fdp-api/licenses/cc0-1.0	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.848039+00
610	anonymous	read	http://localhost:8000/distribution/urban-trees-geojson	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.853405+00
611	anonymous	read	http://localhost:8000/fdp-api/policies/system-default	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.857498+00
612	anonymous	read	http://localhost:8000/fdp-api/licenses/cc-by-4.0	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.860576+00
613	anonymous	read	http://localhost:8000/dataset/urban-tree-inventory	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.865126+00
614	anonymous	read	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.868391+00
615	anonymous	read	http://localhost:8000/distribution/river-temperature-api	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.874443+00
616	anonymous	read	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.87884+00
617	anonymous	read	http://localhost:8000/fdp-api/schemas/catalog	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.882855+00
618	anonymous	read	urn:fdp-shape:meta-metadata	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.886495+00
619	anonymous	read	http://localhost:8000/catalog/environmental-monitoring	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:18.890172+00
620	anonymous	read	http://localhost:8000/fdp-api/schemas/resource	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.898591+00
588	anonymous	read	http://localhost:8000/catalog/soil-and-groundwater-catalog	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:18.75177+00
625	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58#d6728bc894cc7064	read	http://localhost:8000/	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:30.215807+00
622	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58#d6728bc894cc7064	read	http://localhost:8000/catalog/environmental-monitoring/meta	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:30.182019+00
626	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58#d6728bc894cc7064	modify	http://localhost:8000/catalog/environmental-monitoring	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-28 13:53:42.849712+00
627	anonymous	read	http://localhost:8000/catalog	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-29 13:30:42.20732+00
629	anonymous	read	http://localhost:8000/data-service	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-29 13:30:42.226948+00
631	anonymous	read	http://localhost:8000/ontology	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-29 13:30:42.243528+00
628	anonymous	read	http://localhost:8000/dataset	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-29 13:30:42.219303+00
630	anonymous	read	http://localhost:8000/distribution	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-29 13:30:42.233961+00
632	http://localhost:8081/realms/fdp-dev#ba454e0a-0e8c-438d-9443-f0e5c3ec497c#e3b0c44298fc1c14	read	http://localhost:8000/catalog/environmental-monitoring	permit	http://localhost:8000/fdp-api/policies/curated-catalog	2026-10-29 16:20:20.244848+00
586	anonymous	read	http://localhost:8000/fdp-api/policies/curated-catalog	permit	http://localhost:8000/fdp-api/policies/system-default	2026-10-28 13:53:14.49148+00
\.


--
-- Data for Name: index_targets; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.index_targets (id, url, note, created_at, created_by, last_ping_at, last_status_code, last_ok, last_detail) FROM stdin;
\.


--
-- Data for Name: job_state; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.job_state (id) FROM stdin;
\.


--
-- Data for Name: metadata_external_labels; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.metadata_external_labels (iri, language, label, resolved_at, expires_at, source_host) FROM stdin;
\.


--
-- Data for Name: metadata_search; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.metadata_search (record_iri, type_iri, title, description, license, keywords, search_text, state, anon_read, updated_at, language) FROM stdin;
http://localhost:8000/fdp-api/schemas/resource/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/dataset/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/catalog/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/data-service/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/distribution/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/metadata/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/metadata-service/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/schemas/fairdata-point/1	\N	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/policies/system-default	http://www.w3.org/ns/odrl/2/Offer	\N	\N	\N	\N		PUBLISHED	t	2026-08-26 07:41:21.697177+00	english
http://localhost:8000/fdp-api/profiles/resource	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/resource/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/dataset	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/dataset/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/catalog	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/catalog/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/data-service	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/data-service/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/distribution	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/distribution/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/metadata	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/metadata/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/metadata-service	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/metadata-service/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/fairdata-point	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/fdp-api/profiles/fairdata-point/1	http://www.w3.org/ns/dx/prof/Profile	\N	\N	\N	\N		\N	t	\N	english
http://localhost:8000/catalog/dataset-catalog	http://www.w3.org/ns/dcat#Catalog	Dataset catalog	Catalog containing metadata about a number of datasets	\N	\N	'catalog':2,3 'contain':4 'dataset':1,10 'metadata':5 'number':8	DRAFT	t	2026-08-26 10:09:04.623429+00	english
http://localhost:8000/fdp-api/licenses/cc-by-4.0	http://purl.org/dc/terms/LicenseDocument	Creative Commons Attribution 4.0 International	Reuse is allowed for any purpose, including commercial use, as long as the source is credited.	\N	\N	'4.0':4 'allow':8 'attribut':3 'commerci':13 'common':2 'creativ':1 'credit':21 'includ':12 'intern':5 'long':16 'purpos':11 'reus':6 'sourc':19 'use':14	PUBLISHED	t	2026-10-28 11:14:18.815581+00	english
http://localhost:8000/fdp-api/licenses/cc0-1.0	http://purl.org/dc/terms/LicenseDocument	Creative Commons Zero 1.0 Universal (public domain dedication)	All rights waived. The data may be reused without any conditions.	\N	\N	'1.0':4 'common':2 'condit':19 'creativ':1 'data':13 'dedic':8 'domain':7 'may':14 'public':6 'reus':16 'right':10 'univers':5 'waiv':11 'without':17 'zero':3	PUBLISHED	t	2026-10-28 11:14:18.849535+00	english
http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	http://purl.org/dc/terms/LicenseDocument	Creative Commons Attribution-ShareAlike 4.0 International	Reuse is allowed with attribution; derived works must carry the same license.	\N	\N	'4.0':6 'allow':10 'attribut':4,12 'attribution-sharealik':3 'carri':16 'common':2 'creativ':1 'deriv':13 'intern':7 'licens':19 'must':15 'reus':8 'sharealik':5 'work':14	PUBLISHED	t	2026-10-28 11:14:18.88613+00	english
http://localhost:8000/catalog/clinical-research	http://www.w3.org/ns/dcat#Catalog	Clinical research catalog	Metadata about clinical and biobank resources of the fictional Overijssel Medical Research Consortium. The underlying data are not open; the metadata describe how access can be requested.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	restricted access registry clinical biobank	'access':27,32 'biobank':8,35 'catalog':3 'clinic':1,6,34 'consortium':16 'data':19 'describ':25 'fiction':12 'medic':14 'metadata':4,24 'open':22 'overijssel':13 'registri':33 'request':30 'research':2,15 'resourc':9 'restrict':31 'under':18	PUBLISHED	t	2026-10-28 11:14:19.494923+00	english
http://localhost:8000	https://w3id.org/fdp/fdp-o#FAIRDataPoint	FDPneo demonstration deployment	This is a demonstration deployment of the FDPneo.	\N	\N	'demonstr':2,7 'deploy':3,8 'fdpneo':1,11	PUBLISHED	t	2026-10-28 13:19:31.86494+00	english
http://localhost:8000/catalog/environmental-monitoring	http://www.w3.org/ns/dcat#Catalog	Environmental monitoring catalog	Open environmental observation datasets collected by the Twente regional monitoring network. All datasets in this catalog are openly licensed.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	air quality sensors water environment	'air':23 'catalog':3,19 'collect':8 'dataset':7,16 'environ':27 'environment':1,5 'licens':22 'monitor':2,13 'network':14 'observ':6 'open':4,21 'qualiti':24 'region':12 'sensor':25 'twent':11 'water':26	PUBLISHED	t	2026-10-28 13:53:42.896397+00	english
http://localhost:8000/distribution/air-quality-2024-stations	http://www.w3.org/ns/dcat#Distribution	Station metadata, GeoJSON	Coordinates, height above ground and instrument types of the six stations.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	\N	'coordin':4 'geojson':3 'ground':7 'height':5 'instrument':9 'metadata':2 'six':13 'station':1,14 'type':10	PUBLISHED	t	2026-10-28 11:14:19.656395+00	english
http://localhost:8000/dataset/air-quality-enschede-2024	http://www.w3.org/ns/dcat#Dataset	Air quality measurements, Enschede, 2024	Hourly concentrations of nitrogen dioxide, ozone and particulate matter (PM2.5 and PM10) from six fixed monitoring stations in Enschede, January to December 2024. Values are validated and gap-filled according to the network's quality protocol.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	air quality NO2 particulate matter Enschede ozone	'2024':5,28 'accord':36 'air':1,43 'concentr':7 'decemb':27 'dioxid':10 'ensched':4,24,48 'fill':35 'fix':20 'gap':34 'gap-fil':33 'hour':6 'januari':25 'matter':14,47 'measur':3 'monitor':21 'network':39 'nitrogen':9 'no2':45 'ozon':11,49 'particul':13,46 'pm10':17 'pm2.5':15 'protocol':42 'qualiti':2,41,44 'six':19 'station':22 'valid':31 'valu':29	PUBLISHED	t	2026-10-28 11:14:19.673864+00	english
http://localhost:8000/distribution/urban-trees-geojson	http://www.w3.org/ns/dcat#Distribution	Tree inventory, GeoJSON	\N	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	\N	'geojson':3 'inventori':2 'tree':1	PUBLISHED	t	2026-10-28 11:14:19.844348+00	english
http://localhost:8000/dataset/biobank-sample-catalog	http://www.w3.org/ns/dcat#Dataset	Biobank sample catalog (synthetic demo)	Aggregate description of the consortium biobank: sample types, counts per diagnosis group, storage conditions and consent categories. Individual-level data are available only through the data access committee.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	biobank restricted samples consent	'access':33 'aggreg':6 'avail':28 'biobank':1,11,35 'catalog':3 'categori':22 'committe':34 'condit':19 'consent':21,38 'consortium':10 'count':14 'data':26,32 'demo':5 'descript':7 'diagnosi':16 'group':17 'individu':24 'individual-level':23 'level':25 'per':15 'restrict':36 'sampl':2,12,37 'storag':18 'synthet':4 'type':13	PUBLISHED	t	2026-10-28 11:14:19.925695+00	english
http://localhost:8000/dataset/rare-disease-registry	http://www.w3.org/ns/dcat#Dataset	Rare disease registry metadata (synthetic demo)	Variables, coding systems and inclusion criteria of the consortium's rare disease registry. Draft entry, still being completed by the registry team.	\N	registry rare disease variables	'code':8 'complet':24 'consortium':15 'criteria':12 'demo':6 'diseas':2,18,31 'draft':20 'entri':21 'inclus':11 'metadata':4 'rare':1,17,30 'registri':3,19,27,29 'still':22 'synthet':5 'system':9 'team':28 'variabl':7,32	DRAFT	t	2026-10-28 11:14:19.478705+00	english
http://localhost:8000/distribution/river-temperature-csv	http://www.w3.org/ns/dcat#Distribution	Temperature readings, CSV (zip)	\N	http://localhost:8000/fdp-api/licenses/cc0-1.0	\N	'csv':3 'read':2 'temperatur':1 'zip':4	PUBLISHED	t	2026-10-28 11:14:19.723344+00	english
http://localhost:8000/dataset/river-water-temperature	http://www.w3.org/ns/dcat#Dataset	River water temperature time series, Dinkel and Regge, 2015 to 2024	Fifteen-minute water temperature readings from four logger sites on the rivers Dinkel and Regge over ten years, with sensor metadata and maintenance log.	http://localhost:8000/fdp-api/licenses/cc0-1.0	Dinkel Regge time series water temperature river	'2015':9 '2024':11 'dinkel':6,25,37 'fifteen':13 'fifteen-minut':12 'four':19 'log':36 'logger':20 'mainten':35 'metadata':33 'minut':14 'read':17 'regg':8,27,38 'river':1,24,43 'sensor':32 'seri':5,40 'site':21 'temperatur':3,16,42 'ten':29 'time':4,39 'water':2,15,41 'year':30	PUBLISHED	t	2026-10-28 11:14:19.795086+00	english
http://localhost:8000/distribution/biobank-access-request	http://www.w3.org/ns/dcat#Distribution	Access request form	Landing page of the data access committee with the request procedure.	\N	\N	'access':1,9 'committe':10 'data':8 'form':3 'land':4 'page':5 'procedur':14 'request':2,13	PUBLISHED	t	2026-10-28 11:14:19.910951+00	english
http://localhost:8000/distribution/air-quality-2024-csv	http://www.w3.org/ns/dcat#Distribution	Hourly measurements, CSV	One row per station per hour; columns station_id, timestamp, no2, o3, pm25, pm10, flag.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	\N	'column':10 'csv':3 'flag':18 'hour':1,9 'id':12 'measur':2 'no2':14 'o3':15 'one':4 'per':6,8 'pm10':17 'pm25':16 'row':5 'station':7,11 'timestamp':13	PUBLISHED	t	2026-10-28 11:14:19.541291+00	english
http://localhost:8000/distribution/river-temperature-api	http://www.w3.org/ns/dcat#Distribution	Query API, JSON	Paged JSON API over the same readings, filterable by site and date range.	http://localhost:8000/fdp-api/licenses/cc0-1.0	\N	'api':2,6 'date':15 'filter':11 'json':3,5 'page':4 'queri':1 'rang':16 'read':10 'site':13	PUBLISHED	t	2026-10-28 11:14:19.781088+00	english
http://localhost:8000/dataset/urban-tree-inventory	http://www.w3.org/ns/dcat#Dataset	Urban tree inventory, municipality of Enschede	Location, species, trunk circumference, estimated planting year and condition class of every municipally managed tree, as surveyed in 2023 and updated with 2024 removals and plantings.	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	biodiversity trees GIS urban green	'2023':25 '2024':29 'biodivers':33 'circumfer':10 'class':16 'condit':15 'ensched':6 'estim':11 'everi':18 'gis':35 'green':37 'inventori':3 'locat':7 'manag':20 'municip':4,19 'plant':12,32 'remov':30 'speci':8 'survey':23 'tree':2,21,34 'trunk':9 'updat':27 'urban':1,36 'year':13	PUBLISHED	t	2026-10-28 11:14:19.859066+00	english
http://localhost:8000/dataset/noise-levels-enschede-airport-2025	http://www.w3.org/ns/dcat#Dataset	Noise levels around Enschede Airport, 2025 (hourly)	Continuous noise measurements from four stations, aggregated per hour.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	\N	'2025':6 'aggreg':14 'airport':5 'around':3 'continu':8 'ensched':4 'four':12 'hour':7,16 'level':2 'measur':10 'nois':1,9 'per':15 'station':13	PUBLISHED	t	2026-10-28 11:58:38.339904+00	english
http://localhost:8000/catalog/soil-and-groundwater-catalog	http://www.w3.org/ns/dcat#Catalog	Soil and groundwater catalog	Monitoring data on soil quality and groundwater levels in the Twente region, collected by the regional water authority.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	groundwater	'author':22 'catalog':4 'collect':17 'data':6 'groundwat':3,11,23 'level':12 'monitor':5 'qualiti':9 'region':16,20 'soil':1,8 'twent':15 'water':21	PUBLISHED	t	2026-10-28 13:20:48.511703+00	english
http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	http://www.w3.org/ns/dcat#Dataset	Groundwater levels, Twente, 2020 to 2024	Monthly groundwater level readings from 120 monitoring wells across Twente, with well metadata and quality flags. Readings are validated quarterly.	http://localhost:8000/fdp-api/licenses/cc-by-4.0	groundwater	'120':12 '2020':4 '2024':6 'across':15 'flag':22 'groundwat':1,8,27 'level':2,9 'metadata':19 'monitor':13 'month':7 'qualiti':21 'quarter':26 'read':10,23 'twent':3,16 'valid':25 'well':14,18	PUBLISHED	t	2026-10-28 13:21:24.364027+00	english
http://localhost:8000/fdp-api/policies/curated-catalog	http://www.w3.org/ns/odrl/2/Offer	\N	\N	\N	\N		PUBLISHED	t	2026-10-28 13:55:51.396614+00	english
http://localhost:8000/fdp-api/licenses/odc-by-1.0	\N	Open Data Commons Attribution License 1.0	Reuse of the data is allowed with attribution to the source.	\N	\N	'1.0':6 'allow':12 'attribut':4,14 'common':3 'data':2,10 'licens':5 'open':1 'reus':7 'sourc':17	DRAFT	t	2026-10-28 13:49:59.605092+00	english
\.


--
-- Data for Name: metrics_daily; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.metrics_daily (id, bucket, event_type, resource_iri, country_code, region, city, request_count, unique_visitors, latency_ms_sum, status_2xx_count, status_3xx_count, status_4xx_count, status_5xx_count) FROM stdin;
1	2026-08-26	modify	http://localhost:8000/	\N	\N	\N	1	1	128	1	0	0	0
2	2026-08-26	modify	http://localhost:8000/catalog/dataset-catalog	\N	\N	\N	1	1	104	1	0	0	0
3	2026-08-26	modify	http://localhost:8000/fdp-api/search	\N	\N	\N	1	1	34	1	0	0	0
4	2026-08-26	sparql_query	\N	\N	\N	\N	29	4	1046	27	0	2	0
5	2026-08-26	view	http://130.89.182.51/	\N	\N	\N	1	0	7	0	0	1	0
6	2026-08-26	view	http://130.89.182.51:8000/	\N	\N	\N	5	3	147	4	0	1	0
7	2026-08-26	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	22	0	0	1	0
8	2026-08-26	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	3	3	30	0	0	3	0
9	2026-08-26	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	20	0	0	1	0
10	2026-08-26	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	18	0	0	1	0
11	2026-08-26	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	9	0	0	1	0
12	2026-08-26	view	http://130.89.182.51:8000/.well-known/security.txt	\N	\N	\N	1	1	20	0	0	1	0
13	2026-08-26	view	http://172.18.0.5:8000/	\N	\N	\N	1	0	51	1	0	0	0
14	2026-08-26	view	http://172.18.0.5:8000/nice ports,/Trinity.txt.bak	\N	\N	\N	1	0	46	0	0	0	1
15	2026-08-26	view	http://api.ipify.org/	\N	\N	\N	1	1	18	1	0	0	0
16	2026-08-26	view	http://localhost:8000/	\N	\N	\N	14	3	497	13	0	1	0
17	2026-08-26	view	http://localhost:8000/apple-touch-icon.png	\N	\N	\N	1	1	39	0	0	1	0
18	2026-08-26	view	http://localhost:8000/apple-touch-icon-precomposed.png	\N	\N	\N	1	1	38	0	0	1	0
19	2026-08-26	view	http://localhost:8000/catalog/dataset-catalog	\N	\N	\N	2	1	33	1	0	1	0
20	2026-08-26	view	http://localhost:8000/catalog/dataset-catalog/meta	\N	\N	\N	1	1	46	1	0	0	0
21	2026-08-26	view	http://localhost:8000/favicon.ico	\N	\N	\N	1	1	39	0	0	1	0
22	2026-08-26	view	http://localhost:8000/fdp-api/	\N	\N	\N	1	1	23	0	0	1	0
23	2026-08-26	view	http://localhost:8000/fdp-api/catalog/dataset-catalog/expanded	\N	\N	\N	1	1	50	1	0	0	0
24	2026-08-26	view	http://localhost:8000/fdp-api/catalog/spec	\N	\N	\N	1	1	6	1	0	0	0
25	2026-08-26	view	http://localhost:8000/fdp-api/config	\N	\N	\N	3	2	8	3	0	0	0
26	2026-08-26	view	http://localhost:8000/fdp-api/genid-163f11fc2ff7488a8381890d808d2078206-075809BBB30E53A9265EF3216A3EAAA2/expanded	\N	\N	\N	2	1	65	0	0	2	0
27	2026-08-26	view	http://localhost:8000/fdp-api/genid-163f11fc2ff7488a8381890d808d2078206-E1BDFD22112C24DCE31ABBF3F62D9908/expanded	\N	\N	\N	5	1	189	0	0	5	0
28	2026-08-26	view	http://localhost:8000/fdp-api/info	\N	\N	\N	6	3	3	6	0	0	0
29	2026-08-26	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	1	1	54	1	0	0	0
30	2026-08-26	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	5	3	127	5	0	0	0
31	2026-08-26	view	http://localhost:8000/fdp-api/me/dashboard	\N	\N	\N	1	1	19	1	0	0	0
32	2026-08-26	view	http://localhost:8000/fdp-api/me/saved-queries	\N	\N	\N	1	1	13	1	0	0	0
33	2026-08-26	view	http://localhost:8000/fdp-api/page/catalog	\N	\N	\N	3	1	49	3	0	0	0
34	2026-08-26	view	http://localhost:8000/fdp-api/page/dataservice	\N	\N	\N	1	1	5	0	0	1	0
35	2026-08-26	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	6	3	207	6	0	0	0
36	2026-08-26	view	http://localhost:8000/fdp-api/policies/system-default	\N	\N	\N	2	2	12	2	0	0	0
37	2026-08-26	view	http://localhost:8000/fdp-api/readyz	\N	\N	\N	65	2	1287	57	0	8	0
38	2026-08-26	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	7	3	6	7	0	0	0
39	2026-08-26	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	7	3	153	7	0	0	0
40	2026-08-26	view	http://localhost:8000/fdp-api/spec	\N	\N	\N	1	1	5	1	0	0	0
41	2026-08-26	view	http://localhost:8000/genid-163f11fc2ff7488a8381890d808d2078206-075809BBB30E53A9265EF3216A3EAAA2	\N	\N	\N	7	1	148	0	0	7	0
42	2026-08-26	view	http://localhost:8000/genid-163f11fc2ff7488a8381890d808d2078206-075809BBB30E53A9265EF3216A3EAAA2/meta	\N	\N	\N	2	1	66	0	0	2	0
43	2026-08-26	view	http://localhost:8000/genid-163f11fc2ff7488a8381890d808d2078206-E1BDFD22112C24DCE31ABBF3F62D9908	\N	\N	\N	18	1	344	0	0	18	0
44	2026-08-26	view	http://localhost:8000/genid-163f11fc2ff7488a8381890d808d2078206-E1BDFD22112C24DCE31ABBF3F62D9908/meta	\N	\N	\N	5	1	188	0	0	5	0
45	2026-08-27	view	http://130.89.182.51/	\N	\N	\N	3	3	143	3	0	0	0
46	2026-08-27	view	http://130.89.182.51:8000/	\N	\N	\N	11	10	494	11	0	0	0
47	2026-08-27	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	9	0	0	1	0
48	2026-08-27	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	4	4	117	0	0	4	0
49	2026-08-27	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	30	0	0	1	0
50	2026-08-27	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	2	2	23	0	0	2	0
51	2026-08-27	view	http://130.89.182.51/favicon.ico	\N	\N	\N	1	1	7	0	0	1	0
52	2026-08-27	view	http://api.ipify.org/	\N	\N	\N	1	1	10	1	0	0	0
53	2026-08-28	modify	http://130.89.182.51:8000/api/graphql	\N	\N	\N	1	1	6	0	0	1	0
54	2026-08-28	modify	http://130.89.182.51:8000/graphql	\N	\N	\N	1	1	5	0	0	1	0
55	2026-08-28	modify	http://130.89.182.51:8000/graphql/console	\N	\N	\N	1	1	5	0	0	1	0
56	2026-08-28	modify	http://130.89.182.51:8000/v1/graphql	\N	\N	\N	1	1	5	0	0	1	0
57	2026-08-28	view	http://130.89.182.51/	\N	\N	\N	3	2	112	2	0	1	0
58	2026-08-28	view	http://130.89.182.51:8000/	\N	\N	\N	24	19	1173	23	0	1	0
59	2026-08-28	view	http://130.89.182.51:8000/accessKeys.csv	\N	\N	\N	1	1	167	0	0	1	0
60	2026-08-28	view	http://130.89.182.51:8000/actuator/env	\N	\N	\N	2	2	434	0	0	2	0
61	2026-08-28	view	http://130.89.182.51:8000/actuator/logfile	\N	\N	\N	1	1	243	0	0	1	0
62	2026-08-28	view	http://130.89.182.51:8000/admin/.env	\N	\N	\N	1	1	251	0	0	1	0
63	2026-08-28	view	http://130.89.182.51:8000/api/.aws/credentials	\N	\N	\N	1	1	206	0	0	1	0
64	2026-08-28	view	http://130.89.182.51:8000/api/config	\N	\N	\N	1	1	459	0	0	1	0
65	2026-08-28	view	http://130.89.182.51:8000/api/download	\N	\N	\N	2	2	69	0	0	2	0
66	2026-08-28	view	http://130.89.182.51:8000/api/.env	\N	\N	\N	2	2	233	0	0	2	0
67	2026-08-28	view	http://130.89.182.51:8000/api/env	\N	\N	\N	1	1	131	0	0	1	0
68	2026-08-28	view	http://130.89.182.51:8000/api/fetch	\N	\N	\N	2	2	246	0	0	2	0
69	2026-08-28	view	http://130.89.182.51:8000/api/file	\N	\N	\N	1	1	250	0	0	1	0
70	2026-08-28	view	http://130.89.182.51:8000/api/image	\N	\N	\N	2	2	257	0	0	2	0
71	2026-08-28	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	5	0	0	1	0
72	2026-08-28	view	http://130.89.182.51:8000/api/proxy	\N	\N	\N	2	2	171	0	0	2	0
73	2026-08-28	view	http://130.89.182.51:8000/api/v1/credentials	\N	\N	\N	1	1	170	0	0	1	0
74	2026-08-28	view	http://130.89.182.51:8000/api/v1/env	\N	\N	\N	1	1	237	0	0	1	0
75	2026-08-28	view	http://130.89.182.51:8000/api/v1/fetch	\N	\N	\N	2	2	103	0	0	2	0
76	2026-08-28	view	http://130.89.182.51:8000/api/webhook	\N	\N	\N	2	2	46	0	0	2	0
77	2026-08-28	view	http://130.89.182.51:8000/app/api/.env	\N	\N	\N	1	1	158	0	0	1	0
78	2026-08-28	view	http://130.89.182.51:8000/app/.aws/credentials	\N	\N	\N	1	1	170	0	0	1	0
79	2026-08-28	view	http://130.89.182.51:8000/app_dev.php/_profiler/.env	\N	\N	\N	1	1	95	0	0	1	0
80	2026-08-28	view	http://130.89.182.51:8000/app_dev.php/_profiler/phpinfo	\N	\N	\N	1	1	66	0	0	1	0
81	2026-08-28	view	http://130.89.182.51:8000/app/.env	\N	\N	\N	2	2	392	0	0	2	0
82	2026-08-28	view	http://130.89.182.51:8000/app/.env.local	\N	\N	\N	1	1	187	0	0	1	0
83	2026-08-28	view	http://130.89.182.51:8000/app/.env.production	\N	\N	\N	1	1	166	0	0	1	0
84	2026-08-28	view	http://130.89.182.51:8000/application_default_credentials.json	\N	\N	\N	1	1	199	0	0	1	0
85	2026-08-28	view	http://130.89.182.51:8000/application.env	\N	\N	\N	1	1	156	0	0	1	0
86	2026-08-28	view	http://130.89.182.51:8000/application/.env	\N	\N	\N	1	1	104	0	0	1	0
87	2026-08-28	view	http://130.89.182.51:8000/apps/api/.env	\N	\N	\N	1	1	187	0	0	1	0
88	2026-08-28	view	http://130.89.182.51:8000/apps/.env	\N	\N	\N	1	1	128	0	0	1	0
89	2026-08-28	view	http://130.89.182.51:8000/assets../../../.env	\N	\N	\N	1	1	282	0	0	1	0
90	2026-08-28	view	http://130.89.182.51:8000/assets/env.js	\N	\N	\N	1	1	253	0	0	1	0
91	2026-08-28	view	http://130.89.182.51:8000/assets../../../etc/passwd	\N	\N	\N	1	1	409	0	0	1	0
92	2026-08-28	view	http://130.89.182.51:8000/.aws/config	\N	\N	\N	2	2	220	0	0	2	0
93	2026-08-28	view	http://130.89.182.51:8000/aws-config.js	\N	\N	\N	1	1	108	0	0	1	0
94	2026-08-28	view	http://130.89.182.51:8000/aws/credentials	\N	\N	\N	1	1	371	0	0	1	0
95	2026-08-28	view	http://130.89.182.51:8000/..;/..;/.aws/credentials	\N	\N	\N	1	1	139	0	0	1	0
96	2026-08-28	view	http://130.89.182.51:8000/.aws/credentials	\N	\N	\N	3	3	454	0	0	3	0
97	2026-08-28	view	http://130.89.182.51:8000/.aws_credentials	\N	\N	\N	1	1	243	0	0	1	0
98	2026-08-28	view	http://130.89.182.51:8000/.aws/credentials.bak	\N	\N	\N	1	1	212	0	0	1	0
99	2026-08-28	view	http://130.89.182.51:8000/aws-credentials.json	\N	\N	\N	1	1	199	0	0	1	0
100	2026-08-28	view	http://130.89.182.51:8000/aws/credentials.json	\N	\N	\N	1	1	128	0	0	1	0
101	2026-08-28	view	http://130.89.182.51:8000/.aws/credentials.old	\N	\N	\N	1	1	139	0	0	1	0
102	2026-08-28	view	http://130.89.182.51:8000/.aws/ecs-task-credentials	\N	\N	\N	1	1	36	0	0	1	0
103	2026-08-28	view	http://130.89.182.51:8000/aws/.env	\N	\N	\N	1	1	158	0	0	1	0
104	2026-08-28	view	http://130.89.182.51:8000/aws/.env.production	\N	\N	\N	1	1	138	0	0	1	0
105	2026-08-28	view	http://130.89.182.51:8000/aws-exports.js	\N	\N	\N	1	1	234	0	0	1	0
106	2026-08-28	view	http://130.89.182.51:8000/aws/iam/temporary-credentials	\N	\N	\N	1	1	156	0	0	1	0
107	2026-08-28	view	http://130.89.182.51:8000/aws.json	\N	\N	\N	1	1	147	0	0	1	0
108	2026-08-28	view	http://130.89.182.51:8000/azure-pipelines.yml	\N	\N	\N	1	1	137	0	0	1	0
109	2026-08-28	view	http://130.89.182.51:8000/backend/.aws/credentials	\N	\N	\N	1	1	226	0	0	1	0
110	2026-08-28	view	http://130.89.182.51:8000/backend/aws.json	\N	\N	\N	1	1	43	0	0	1	0
111	2026-08-28	view	http://130.89.182.51:8000/backend/.env	\N	\N	\N	1	1	270	0	0	1	0
112	2026-08-28	view	http://130.89.182.51:8000/backend/.env.local	\N	\N	\N	1	1	237	0	0	1	0
113	2026-08-28	view	http://130.89.182.51:8000/backend/.env.production	\N	\N	\N	1	1	172	0	0	1	0
114	2026-08-28	view	http://130.89.182.51:8000/back/.env	\N	\N	\N	1	1	135	0	0	1	0
115	2026-08-28	view	http://130.89.182.51:8000/backup/.env	\N	\N	\N	1	1	133	0	0	1	0
116	2026-08-28	view	http://130.89.182.51:8000/bitbucket-pipelines.yml	\N	\N	\N	1	1	279	0	0	1	0
117	2026-08-28	view	http://130.89.182.51:8000/.circleci/config.yml	\N	\N	\N	1	1	210	0	0	1	0
118	2026-08-28	view	http://130.89.182.51:8000/claude_desktop_config.json	\N	\N	\N	1	1	238	0	0	1	0
119	2026-08-28	view	http://130.89.182.51:8000/client/.env	\N	\N	\N	1	1	126	0	0	1	0
120	2026-08-28	view	http://130.89.182.51:8000/cloudbuild.yaml	\N	\N	\N	1	1	178	0	0	1	0
121	2026-08-28	view	http://130.89.182.51:8000/config/aws.json	\N	\N	\N	1	1	132	0	0	1	0
122	2026-08-28	view	http://130.89.182.51:8000/config.env	\N	\N	\N	1	1	151	0	0	1	0
123	2026-08-28	view	http://130.89.182.51:8000/config/.env	\N	\N	\N	1	1	165	0	0	1	0
124	2026-08-28	view	http://130.89.182.51:8000/config/env.js	\N	\N	\N	1	1	180	0	0	1	0
125	2026-08-28	view	http://130.89.182.51:8000/config/.env.local	\N	\N	\N	1	1	235	0	0	1	0
126	2026-08-28	view	http://130.89.182.51:8000/config/.env.php	\N	\N	\N	1	1	62	0	0	1	0
127	2026-08-28	view	http://130.89.182.51:8000/.config/gcloud/application_default_credentials.json	\N	\N	\N	1	1	17	0	0	1	0
128	2026-08-28	view	http://130.89.182.51:8000/config/gcp-credentials.json	\N	\N	\N	1	1	214	0	0	1	0
129	2026-08-28	view	http://130.89.182.51:8000/config/jenkins.conf	\N	\N	\N	1	1	149	0	0	1	0
130	2026-08-28	view	http://130.89.182.51:8000/config/jenkins.xml	\N	\N	\N	1	1	223	0	0	1	0
131	2026-08-28	view	http://130.89.182.51:8000/config.js	\N	\N	\N	1	1	171	0	0	1	0
132	2026-08-28	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	5	0	0	1	0
133	2026-08-28	view	http://130.89.182.51:8000/config.php	\N	\N	\N	1	1	14	0	0	1	0
134	2026-08-28	view	http://130.89.182.51:8000/core/.env	\N	\N	\N	1	1	263	0	0	1	0
135	2026-08-28	view	http://130.89.182.51:8000/credentials	\N	\N	\N	1	1	119	0	0	1	0
136	2026-08-28	view	http://130.89.182.51:8000/credentials.json	\N	\N	\N	1	1	105	0	0	1	0
137	2026-08-28	view	http://130.89.182.51:8000/crm/.env	\N	\N	\N	1	1	120	0	0	1	0
138	2026-08-28	view	http://130.89.182.51:8000/.cursor/mcp.json	\N	\N	\N	1	1	153	0	0	1	0
139	2026-08-28	view	http://130.89.182.51:8000/debug/default/view	\N	\N	\N	1	1	252	0	0	1	0
140	2026-08-28	view	http://130.89.182.51:8000/debug.log	\N	\N	\N	1	1	72	0	0	1	0
141	2026-08-28	view	http://130.89.182.51:8000/debug/pprof/cmdline	\N	\N	\N	1	1	169	0	0	1	0
142	2026-08-28	view	http://130.89.182.51:8000/development/.env	\N	\N	\N	1	1	192	0	0	1	0
143	2026-08-28	view	http://130.89.182.51:8000/dev/.env	\N	\N	\N	1	1	192	0	0	1	0
144	2026-08-28	view	http://130.89.182.51:8000/docker-compose.yml	\N	\N	\N	1	1	10	0	0	1	0
145	2026-08-28	view	http://130.89.182.51:8000/docker/.env	\N	\N	\N	1	1	167	0	0	1	0
146	2026-08-28	view	http://130.89.182.51:8000/.docker.env	\N	\N	\N	1	1	245	0	0	1	0
147	2026-08-28	view	http://130.89.182.51:8000/.docker/.env	\N	\N	\N	1	1	111	0	0	1	0
148	2026-08-28	view	http://130.89.182.51:8000/download	\N	\N	\N	3	3	884	0	0	3	0
149	2026-08-28	view	http://130.89.182.51:8000/.drone.yml	\N	\N	\N	1	1	254	0	0	1	0
150	2026-08-28	view	http://130.89.182.51:8000/env	\N	\N	\N	1	1	199	0	0	1	0
151	2026-08-28	view	http://130.89.182.51:8000/.env	\N	\N	\N	3	3	588	0	0	3	0
152	2026-08-28	view	http://130.89.182.51:8000/.env~	\N	\N	\N	1	1	141	0	0	1	0
153	2026-08-28	view	http://130.89.182.51:8000/.env.anthropic	\N	\N	\N	1	1	277	0	0	1	0
154	2026-08-28	view	http://130.89.182.51:8000/.env.aws	\N	\N	\N	1	1	217	0	0	1	0
155	2026-08-28	view	http://130.89.182.51:8000/.env.azure	\N	\N	\N	1	1	196	0	0	1	0
156	2026-08-28	view	http://130.89.182.51:8000/.env.backup	\N	\N	\N	1	1	63	0	0	1	0
157	2026-08-28	view	http://130.89.182.51:8000/.env.bak	\N	\N	\N	2	2	71	0	0	2	0
158	2026-08-28	view	http://130.89.182.51:8000/.env.ci	\N	\N	\N	1	1	251	0	0	1	0
159	2026-08-28	view	http://130.89.182.51:8000/.env.dev	\N	\N	\N	1	1	37	0	0	1	0
160	2026-08-28	view	http://130.89.182.51:8000/.env.development	\N	\N	\N	1	1	64	0	0	1	0
161	2026-08-28	view	http://130.89.182.51:8000/.env.docker	\N	\N	\N	1	1	240	0	0	1	0
162	2026-08-28	view	http://130.89.182.51:8000/.env.example	\N	\N	\N	1	1	289	0	0	1	0
163	2026-08-28	view	http://130.89.182.51:8000/.env.gcp	\N	\N	\N	1	1	298	0	0	1	0
164	2026-08-28	view	http://130.89.182.51:8000/environment.js	\N	\N	\N	1	1	213	0	0	1	0
165	2026-08-28	view	http://130.89.182.51:8000/env.js	\N	\N	\N	1	1	191	0	0	1	0
166	2026-08-28	view	http://130.89.182.51:8000/.env.js	\N	\N	\N	1	1	230	0	0	1	0
167	2026-08-28	view	http://130.89.182.51:8000/__env.js	\N	\N	\N	1	1	224	0	0	1	0
168	2026-08-28	view	http://130.89.182.51:8000/env.json	\N	\N	\N	1	1	136	0	0	1	0
169	2026-08-28	view	http://130.89.182.51:8000/.env.json	\N	\N	\N	1	1	107	0	0	1	0
170	2026-08-28	view	http://130.89.182.51:8000/.env.local	\N	\N	\N	1	1	446	0	0	1	0
171	2026-08-28	view	http://130.89.182.51:8000/.env.old	\N	\N	\N	1	1	55	0	0	1	0
172	2026-08-28	view	http://130.89.182.51:8000/.env.openai	\N	\N	\N	1	1	201	0	0	1	0
173	2026-08-28	view	http://130.89.182.51:8000/.env.orig	\N	\N	\N	1	1	231	0	0	1	0
174	2026-08-28	view	http://130.89.182.51:8000/.env.php	\N	\N	\N	1	1	180	0	0	1	0
175	2026-08-28	view	http://130.89.182.51:8000/.env.prod	\N	\N	\N	1	1	182	0	0	1	0
176	2026-08-28	view	http://130.89.182.51:8000/.env.prod.bak	\N	\N	\N	1	1	150	0	0	1	0
177	2026-08-28	view	http://130.89.182.51:8000/.env.production	\N	\N	\N	2	2	61	0	0	2	0
178	2026-08-28	view	http://130.89.182.51:8000/.env.production.bak	\N	\N	\N	1	1	245	0	0	1	0
179	2026-08-28	view	http://130.89.182.51:8000/.env.production.local	\N	\N	\N	1	1	203	0	0	1	0
180	2026-08-28	view	http://130.89.182.51:8000/.env.sample	\N	\N	\N	1	1	121	0	0	1	0
181	2026-08-28	view	http://130.89.182.51:8000/.env.save	\N	\N	\N	1	1	214	0	0	1	0
182	2026-08-28	view	http://130.89.182.51:8000/.env.staging	\N	\N	\N	1	1	63	0	0	1	0
183	2026-08-28	view	http://130.89.182.51:8000/.env.swp	\N	\N	\N	1	1	261	0	0	1	0
184	2026-08-28	view	http://130.89.182.51:8000/.env.test	\N	\N	\N	1	1	6	0	0	1	0
185	2026-08-28	view	http://130.89.182.51:8000/.env.yaml	\N	\N	\N	1	1	287	0	0	1	0
186	2026-08-28	view	http://130.89.182.51:8000/.env.yml	\N	\N	\N	1	1	266	0	0	1	0
187	2026-08-28	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	4	4	33	0	0	4	0
188	2026-08-28	view	http://130.89.182.51:8000/fetch	\N	\N	\N	21	20	1526	0	0	21	0
189	2026-08-28	view	http://130.89.182.51:8000/files../.env	\N	\N	\N	1	1	199	0	0	1	0
190	2026-08-28	view	http://130.89.182.51:8000/files../etc/passwd	\N	\N	\N	1	1	451	0	0	1	0
191	2026-08-28	view	http://130.89.182.51:8000/firebase-adminsdk.json	\N	\N	\N	1	1	166	0	0	1	0
192	2026-08-28	view	http://130.89.182.51:8000/firebase-config.json	\N	\N	\N	1	1	219	0	0	1	0
193	2026-08-28	view	http://130.89.182.51:8000/firebase-credentials.json	\N	\N	\N	1	1	221	0	0	1	0
194	2026-08-28	view	http://130.89.182.51:8000/firebase-service-account.json	\N	\N	\N	1	1	151	0	0	1	0
195	2026-08-28	view	http://130.89.182.51:8000/frontend/.env	\N	\N	\N	1	1	218	0	0	1	0
196	2026-08-28	view	http://130.89.182.51:8000/@fs/app/.aws/credentials	\N	\N	\N	1	1	212	0	0	1	0
197	2026-08-28	view	http://130.89.182.51:8000/@fs/app/.env	\N	\N	\N	1	1	200	0	0	1	0
198	2026-08-28	view	http://130.89.182.51:8000/@fs/../.env	\N	\N	\N	1	1	457	0	0	1	0
199	2026-08-28	view	http://130.89.182.51:8000/@fs/.env	\N	\N	\N	1	1	172	0	0	1	0
200	2026-08-28	view	http://130.89.182.51:8000/@fs/etc/passwd	\N	\N	\N	2	2	908	0	0	2	0
201	2026-08-28	view	http://130.89.182.51:8000/@fs/home/ec2-user/.aws/credentials	\N	\N	\N	2	2	559	0	0	2	0
202	2026-08-28	view	http://130.89.182.51:8000/@fs/home/node/.aws/credentials	\N	\N	\N	1	1	234	0	0	1	0
203	2026-08-28	view	http://130.89.182.51:8000/@fs/home/ubuntu/.aws/credentials	\N	\N	\N	1	1	152	0	0	1	0
204	2026-08-28	view	http://130.89.182.51:8000/@fs/proc/1/environ	\N	\N	\N	2	2	595	0	0	2	0
205	2026-08-28	view	http://130.89.182.51:8000/@fs/proc/self/cwd/.aws/credentials	\N	\N	\N	1	1	197	0	0	1	0
206	2026-08-28	view	http://130.89.182.51:8000/@fs/proc/self/cwd/.env	\N	\N	\N	1	1	243	0	0	1	0
207	2026-08-28	view	http://130.89.182.51:8000/@fs/proc/self/environ	\N	\N	\N	3	3	993	0	0	3	0
208	2026-08-28	view	http://130.89.182.51:8000/@fs/root/.aws/config	\N	\N	\N	1	1	181	0	0	1	0
209	2026-08-28	view	http://130.89.182.51:8000/@fs/root/.aws/credentials	\N	\N	\N	2	2	534	0	0	2	0
210	2026-08-28	view	http://130.89.182.51:8000/@fs/root/.env	\N	\N	\N	1	1	142	0	0	1	0
211	2026-08-28	view	http://130.89.182.51:8000/.gcloud/credentials	\N	\N	\N	1	1	175	0	0	1	0
212	2026-08-28	view	http://130.89.182.51:8000/.gcloud/credentials.json	\N	\N	\N	1	1	6	0	0	1	0
213	2026-08-28	view	http://130.89.182.51:8000/gcp-credentials.json	\N	\N	\N	1	1	147	0	0	1	0
214	2026-08-28	view	http://130.89.182.51:8000/gcp-service-account.json	\N	\N	\N	1	1	162	0	0	1	0
215	2026-08-28	view	http://130.89.182.51:8000/.git/config	\N	\N	\N	2	2	201	0	0	2	0
216	2026-08-28	view	http://130.89.182.51:8000/.git-credentials	\N	\N	\N	1	1	174	0	0	1	0
217	2026-08-28	view	http://130.89.182.51:8000/.git/HEAD	\N	\N	\N	2	2	405	0	0	2	0
218	2026-08-28	view	http://130.89.182.51:8000/.github/.env	\N	\N	\N	1	1	186	0	0	1	0
219	2026-08-28	view	http://130.89.182.51:8000/.github/secrets.env	\N	\N	\N	1	1	174	0	0	1	0
220	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/build.yml	\N	\N	\N	1	1	170	0	0	1	0
221	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/ci.yml	\N	\N	\N	1	1	159	0	0	1	0
222	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/deploy.yml	\N	\N	\N	1	1	125	0	0	1	0
223	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/docker.yml	\N	\N	\N	1	1	256	0	0	1	0
224	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/main.yml	\N	\N	\N	1	1	240	0	0	1	0
225	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/publish.yml	\N	\N	\N	1	1	133	0	0	1	0
226	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/release.yml	\N	\N	\N	1	1	155	0	0	1	0
227	2026-08-28	view	http://130.89.182.51:8000/.github/workflows/test.yml	\N	\N	\N	1	1	297	0	0	1	0
228	2026-08-28	view	http://130.89.182.51:8000/.gitlab-ci.yml	\N	\N	\N	1	1	163	0	0	1	0
229	2026-08-28	view	http://130.89.182.51:8000/google-credentials.json	\N	\N	\N	1	1	168	0	0	1	0
230	2026-08-28	view	http://130.89.182.51:8000/google-service-account.json	\N	\N	\N	1	1	216	0	0	1	0
231	2026-08-28	view	http://130.89.182.51:8000/google-services.json	\N	\N	\N	1	1	221	0	0	1	0
232	2026-08-28	view	http://130.89.182.51:8000/health	\N	\N	\N	1	1	27	0	0	1	0
233	2026-08-28	view	http://130.89.182.51:8000/.hermes/.env	\N	\N	\N	1	1	138	0	0	1	0
234	2026-08-28	view	http://130.89.182.51:8000/home/circleci/.aws/credentials	\N	\N	\N	1	1	129	0	0	1	0
235	2026-08-28	view	http://130.89.182.51:8000/home/ec2-user/.aws/credentials	\N	\N	\N	1	1	169	0	0	1	0
236	2026-08-28	view	http://130.89.182.51:8000/home/gitlab-runner/.aws/credentials	\N	\N	\N	1	1	10	0	0	1	0
237	2026-08-28	view	http://130.89.182.51:8000/home/node/.aws/credentials	\N	\N	\N	1	1	262	0	0	1	0
238	2026-08-28	view	http://130.89.182.51:8000/home/runner/.aws/credentials	\N	\N	\N	1	1	12	0	0	1	0
239	2026-08-28	view	http://130.89.182.51:8000/home/ubuntu/.aws/credentials	\N	\N	\N	1	1	306	0	0	1	0
240	2026-08-28	view	http://130.89.182.51:8000/html/.env	\N	\N	\N	1	1	150	0	0	1	0
241	2026-08-28	view	http://130.89.182.51:8000/image	\N	\N	\N	2	2	19	0	0	2	0
242	2026-08-28	view	http://130.89.182.51:8000/internal-api/iam/credentials	\N	\N	\N	1	1	136	0	0	1	0
243	2026-08-28	view	http://130.89.182.51:8000/internal/.env	\N	\N	\N	1	1	216	0	0	1	0
244	2026-08-28	view	http://130.89.182.51:8000/internal/.env.production	\N	\N	\N	1	1	244	0	0	1	0
245	2026-08-28	view	http://130.89.182.51:8000/jenkins/config.xml	\N	\N	\N	1	1	129	0	0	1	0
246	2026-08-28	view	http://130.89.182.51:8000/.jenkins/config.xml	\N	\N	\N	1	1	152	0	0	1	0
247	2026-08-28	view	http://130.89.182.51:8000/jenkins/config.xml.bak	\N	\N	\N	1	1	215	0	0	1	0
248	2026-08-28	view	http://130.89.182.51:8000/jenkins/credentials.xml	\N	\N	\N	1	1	269	0	0	1	0
249	2026-08-28	view	http://130.89.182.51:8000/Jenkinsfile	\N	\N	\N	1	1	17	0	0	1	0
250	2026-08-28	view	http://130.89.182.51:8000/jenkins/Jenkinsfile	\N	\N	\N	1	1	194	0	0	1	0
251	2026-08-28	view	http://130.89.182.51:8000/jenkins/secrets/master.key	\N	\N	\N	1	1	189	0	0	1	0
252	2026-08-28	view	http://130.89.182.51:8000/k8s/eks/credentials	\N	\N	\N	1	1	117	0	0	1	0
253	2026-08-28	view	http://130.89.182.51:8000/laravel/.env	\N	\N	\N	1	1	188	0	0	1	0
254	2026-08-28	view	http://130.89.182.51:8000/management/env	\N	\N	\N	1	1	90	0	0	1	0
255	2026-08-28	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	5	0	0	1	0
256	2026-08-28	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	3	0	0	1	0
257	2026-08-28	view	http://130.89.182.51:8000/.mcp.json	\N	\N	\N	1	1	419	0	0	1	0
258	2026-08-28	view	http://130.89.182.51:8000/media../.env	\N	\N	\N	1	1	442	0	0	1	0
259	2026-08-28	view	http://130.89.182.51:8000/media../etc/passwd	\N	\N	\N	1	1	412	0	0	1	0
260	2026-08-28	view	http://130.89.182.51:8000/_next/../.aws/credentials	\N	\N	\N	1	1	278	0	0	1	0
261	2026-08-28	view	http://130.89.182.51:8000/_next/../.env	\N	\N	\N	1	1	260	0	0	1	0
262	2026-08-28	view	http://130.89.182.51:8000/_next/static/../.env	\N	\N	\N	1	1	287	0	0	1	0
263	2026-08-28	view	http://130.89.182.51:8000/_nuxt/../.env	\N	\N	\N	1	1	213	0	0	1	0
264	2026-08-28	view	http://130.89.182.51:8000/old/.env	\N	\N	\N	1	1	322	0	0	1	0
265	2026-08-28	view	http://130.89.182.51:8000/.openclaw/.env	\N	\N	\N	1	1	156	0	0	1	0
266	2026-08-28	view	http://130.89.182.51:8000/phpinfo.php	\N	\N	\N	1	1	234	0	0	1	0
267	2026-08-28	view	http://130.89.182.51:8000/preview	\N	\N	\N	2	2	53	0	0	2	0
268	2026-08-28	view	http://130.89.182.51:8000/private/.env.production	\N	\N	\N	1	1	176	0	0	1	0
269	2026-08-28	view	http://130.89.182.51:8000/proc/self/environ	\N	\N	\N	1	1	406	0	0	1	0
270	2026-08-28	view	http://130.89.182.51:8000/prod/.env	\N	\N	\N	1	1	145	0	0	1	0
271	2026-08-28	view	http://130.89.182.51:8000/production/.env	\N	\N	\N	1	1	210	0	0	1	0
272	2026-08-28	view	http://130.89.182.51:8000/_profiler/phpinfo	\N	\N	\N	1	1	254	0	0	1	0
273	2026-08-28	view	http://130.89.182.51:8000/_profiler/phpinfo.php	\N	\N	\N	1	1	142	0	0	1	0
274	2026-08-28	view	http://130.89.182.51:8000/proxy	\N	\N	\N	2	2	12	0	0	2	0
275	2026-08-28	view	http://130.89.182.51:8000/public/.aws/credentials	\N	\N	\N	1	1	115	0	0	1	0
276	2026-08-28	view	http://130.89.182.51:8000/public/.env	\N	\N	\N	1	1	153	0	0	1	0
277	2026-08-28	view	http://130.89.182.51:8000/public/env.js	\N	\N	\N	1	1	187	0	0	1	0
278	2026-08-28	view	http://130.89.182.51:8000/read	\N	\N	\N	3	3	892	0	0	3	0
279	2026-08-28	view	http://130.89.182.51:8000/redirect	\N	\N	\N	2	2	109	0	0	2	0
280	2026-08-28	view	http://130.89.182.51:8000/root/.aws/credentials	\N	\N	\N	1	1	169	0	0	1	0
281	2026-08-28	view	http://130.89.182.51:8000/runtime-config.js	\N	\N	\N	1	1	215	0	0	1	0
282	2026-08-28	view	http://130.89.182.51:8000/s3/.aws/credentials	\N	\N	\N	1	1	116	0	0	1	0
283	2026-08-28	view	http://130.89.182.51:8000/s3-credentials.json	\N	\N	\N	1	1	122	0	0	1	0
284	2026-08-28	view	http://130.89.182.51:8000/screenshot	\N	\N	\N	2	2	16	0	0	2	0
285	2026-08-28	view	http://130.89.182.51:8000/secrets/aws.json	\N	\N	\N	1	1	30	0	0	1	0
286	2026-08-28	view	http://130.89.182.51:8000/secrets.env	\N	\N	\N	1	1	102	0	0	1	0
287	2026-08-28	view	http://130.89.182.51:8000/secrets/gcp.json	\N	\N	\N	1	1	177	0	0	1	0
288	2026-08-28	view	http://130.89.182.51:8000/secrets.json	\N	\N	\N	1	1	178	0	0	1	0
289	2026-08-28	view	http://130.89.182.51:8000/sendgrid/.env.php	\N	\N	\N	1	1	125	0	0	1	0
290	2026-08-28	view	http://130.89.182.51:8000/server/.env	\N	\N	\N	1	1	230	0	0	1	0
291	2026-08-28	view	http://130.89.182.51:8000/service-account.json	\N	\N	\N	1	1	18	0	0	1	0
292	2026-08-28	view	http://130.89.182.51:8000/serviceAccountKey.json	\N	\N	\N	2	2	230	0	0	2	0
293	2026-08-28	view	http://130.89.182.51:8000/services/.env	\N	\N	\N	1	1	238	0	0	1	0
294	2026-08-28	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	9	0	0	1	0
295	2026-08-28	view	http://130.89.182.51:8000/src/.env	\N	\N	\N	1	1	177	0	0	1	0
296	2026-08-28	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
297	2026-08-28	view	http://130.89.182.51:8000/staging/.env	\N	\N	\N	1	1	132	0	0	1	0
298	2026-08-28	view	http://130.89.182.51:8000/static../.aws/credentials	\N	\N	\N	1	1	427	0	0	1	0
299	2026-08-28	view	http://130.89.182.51:8000/static../.env	\N	\N	\N	1	1	24	0	0	1	0
300	2026-08-28	view	http://130.89.182.51:8000/static../etc/passwd	\N	\N	\N	1	1	402	0	0	1	0
301	2026-08-28	view	http://130.89.182.51:8000/static../proc/self/environ	\N	\N	\N	1	1	207	0	0	1	0
302	2026-08-28	view	http://130.89.182.51:8000/storage/logs/laravel.log	\N	\N	\N	1	1	256	0	0	1	0
303	2026-08-28	view	http://130.89.182.51:8000/.streamlit/secrets.toml	\N	\N	\N	1	1	16	0	0	1	0
304	2026-08-28	view	http://130.89.182.51:8000/.supabase/.env	\N	\N	\N	1	1	128	0	0	1	0
305	2026-08-28	view	http://130.89.182.51:8000/terraform.tfstate	\N	\N	\N	1	1	15	0	0	1	0
306	2026-08-28	view	http://130.89.182.51:8000/.travis.yml	\N	\N	\N	1	1	206	0	0	1	0
307	2026-08-28	view	http://130.89.182.51:8000/twilio/.env.php	\N	\N	\N	1	1	106	0	0	1	0
308	2026-08-28	view	http://130.89.182.51:8000/v1/.env	\N	\N	\N	1	1	85	0	0	1	0
309	2026-08-28	view	http://130.89.182.51:8000/v1/models	\N	\N	\N	2	2	31	0	0	2	0
310	2026-08-28	view	http://130.89.182.51:8000/v2/.env	\N	\N	\N	1	1	162	0	0	1	0
311	2026-08-28	view	http://130.89.182.51:8000/var/www/.env	\N	\N	\N	1	1	208	0	0	1	0
312	2026-08-28	view	http://130.89.182.51:8000/vendor/.aws/credentials	\N	\N	\N	1	1	128	0	0	1	0
313	2026-08-28	view	http://130.89.182.51:8000/vendor/aws/credentials	\N	\N	\N	1	1	119	0	0	1	0
314	2026-08-28	view	http://130.89.182.51:8000/.vercel/.env	\N	\N	\N	1	1	183	0	0	1	0
315	2026-08-28	view	http://130.89.182.51:8000/@vite/env	\N	\N	\N	1	1	368	0	0	1	0
316	2026-08-28	view	http://130.89.182.51:8000/.vite/manifest.json	\N	\N	\N	1	1	250	0	0	1	0
317	2026-08-28	view	http://130.89.182.51:8000/web/.env	\N	\N	\N	1	1	269	0	0	1	0
318	2026-08-28	view	http://130.89.182.51:8000/webhook	\N	\N	\N	2	2	61	0	0	2	0
319	2026-08-28	view	http://130.89.182.51:8000/.well-known/credentials.json	\N	\N	\N	1	1	135	0	0	1	0
320	2026-08-28	view	http://130.89.182.51:8000/wp-content/debug.log	\N	\N	\N	1	1	159	0	0	1	0
321	2026-08-28	view	http://130.89.182.51:8000/x7f9q2z	\N	\N	\N	1	1	22	0	0	1	0
322	2026-08-28	view	http://api.ipify.org/	\N	\N	\N	1	1	25	1	0	0	0
323	2026-08-29	view	http://130.89.182.51/	\N	\N	\N	2	2	64	2	0	0	0
324	2026-08-29	view	http://130.89.182.51:8000/	\N	\N	\N	10	9	335	9	0	1	0
325	2026-08-29	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	1	0	0	1	0
326	2026-08-29	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	3	0	0	1	0
327	2026-08-29	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	5	5	36	0	0	5	0
328	2026-08-29	view	http://130.89.182.51:8000/health	\N	\N	\N	1	1	1	0	0	1	0
329	2026-08-29	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	2	0	0	1	0
330	2026-08-29	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	2	0	0	1	0
331	2026-08-29	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	7	0	0	1	0
332	2026-08-29	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	8	0	0	1	0
333	2026-08-29	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
334	2026-08-29	view	http://130.89.182.51:8000/v1/models	\N	\N	\N	1	1	12	0	0	1	0
335	2026-08-29	view	http://130.89.182.51:8000/.well-known/security.txt	\N	\N	\N	1	1	12	0	0	1	0
336	2026-08-29	view	http://api.ipify.org/	\N	\N	\N	1	1	23	1	0	0	0
337	2026-08-30	view	http://130.89.182.51/	\N	\N	\N	5	4	207	4	0	1	0
338	2026-08-30	view	http://130.89.182.51:8000/	\N	\N	\N	9	8	423	8	0	1	0
339	2026-08-30	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	16	0	0	1	0
340	2026-08-30	view	http://130.89.182.51:8000/c/	\N	\N	\N	1	1	64	0	0	1	0
341	2026-08-30	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	5	5	97	0	0	5	0
342	2026-08-30	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	15	0	0	1	0
343	2026-08-30	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	14	0	0	1	0
344	2026-08-30	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
345	2026-08-30	view	http://130.89.182.51:8000/.well-known/security.txt	\N	\N	\N	1	1	70	0	0	1	0
346	2026-08-30	view	http://130.89.182.51/favicon.ico	\N	\N	\N	1	1	6	0	0	1	0
347	2026-08-30	view	http://api.ipify.org/	\N	\N	\N	1	1	21	1	0	0	0
348	2026-08-31	sparql_query	\N	\N	\N	\N	4	1	85	0	0	4	0
349	2026-08-31	view	http://130.89.182.51/	\N	\N	\N	2	2	115	2	0	0	0
350	2026-08-31	view	http://130.89.182.51:8000/	\N	\N	\N	9	7	590	8	0	1	0
351	2026-08-31	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	6	0	0	1	0
352	2026-08-31	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	3	3	44	0	0	3	0
353	2026-08-31	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	6	0	0	1	0
354	2026-08-31	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	5	0	0	1	0
355	2026-08-31	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	14	0	0	1	0
356	2026-08-31	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
357	2026-08-31	view	http://172.18.0.5:8000/	\N	\N	\N	1	0	22	1	0	0	0
358	2026-08-31	view	http://172.18.0.5:8000/nice ports,/Trinity.txt.bak	\N	\N	\N	1	0	61	0	0	0	1
359	2026-08-31	view	http://api.ipify.org/	\N	\N	\N	1	1	37	1	0	0	0
360	2026-08-31	view	http://localhost:8000/	\N	\N	\N	2	1	85	0	0	2	0
361	2026-08-31	view	http://localhost:8000/fdp-api/readyz	\N	\N	\N	10	5	492	0	0	10	0
362	2026-08-31	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	2	1	1	0	0	2	0
363	2026-08-31	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	2	1	0	0	0	2	0
364	2026-09-01	sparql_query	\N	\N	\N	\N	3	2	540	1	0	2	0
365	2026-09-01	view	http://130.89.182.51/	\N	\N	\N	5	4	293	4	0	1	0
366	2026-09-01	view	http://130.89.182.51:8000/	\N	\N	\N	7	7	374	7	0	0	0
367	2026-09-01	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	2	2	22	0	0	2	0
368	2026-09-01	view	http://130.89.182.51:8000/v1/models	\N	\N	\N	3	2	194	0	0	3	0
369	2026-09-01	view	http://130.89.182.51:8000/.well-known/security.txt	\N	\N	\N	1	1	13	0	0	1	0
370	2026-09-01	view	http://localhost:8000/	\N	\N	\N	3	2	363	2	0	1	0
371	2026-09-01	view	http://localhost:8000/fdp-api/info	\N	\N	\N	3	1	6	3	0	0	0
372	2026-09-01	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	1	1	98	1	0	0	0
373	2026-09-01	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	3	1	257	3	0	0	0
374	2026-09-01	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	3	1	308	3	0	0	0
375	2026-09-01	view	http://localhost:8000/fdp-api/readyz	\N	\N	\N	1	1	222	0	0	1	0
376	2026-09-01	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	1	1	219	0	0	1	0
377	2026-09-01	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	1	1	220	0	0	1	0
378	2026-09-02	sparql_query	\N	\N	\N	\N	2	2	136	2	0	0	0
379	2026-09-02	view	http://130.89.182.51/	\N	\N	\N	1	1	86	1	0	0	0
380	2026-09-02	view	http://130.89.182.51:8000/	\N	\N	\N	3	3	234	3	0	0	0
381	2026-09-02	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	3	3	66	0	0	3	0
382	2026-09-02	view	http://130.89.182.51:8000/wiki	\N	\N	\N	2	2	52	0	0	2	0
383	2026-09-02	view	http://172.18.0.3:8000/	\N	\N	\N	1	0	94	1	0	0	0
384	2026-09-02	view	http://172.18.0.3:8000/nice ports,/Trinity.txt.bak	\N	\N	\N	1	0	37	0	0	0	1
385	2026-09-02	view	http://api.ipify.org/	\N	\N	\N	1	1	60	1	0	0	0
386	2026-09-02	view	http://localhost:8000/	\N	\N	\N	2	1	179	2	0	0	0
387	2026-09-02	view	http://localhost:8000/fdp-api/info	\N	\N	\N	10	3	10	10	0	0	0
388	2026-09-02	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	1	1	98	1	0	0	0
389	2026-09-02	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	9	3	597	9	0	0	0
390	2026-09-02	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	9	3	778	9	0	0	0
391	2026-09-04	view	http://130.89.182.51:8000/	\N	\N	\N	4	2	399	4	0	0	0
392	2026-09-04	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	3	2	55	0	0	3	0
393	2026-09-04	view	http://130.89.182.51:8000/login	\N	\N	\N	1	1	37	0	0	1	0
394	2026-09-04	view	http://130.89.182.51:8000/security.txt	\N	\N	\N	1	1	61	0	0	1	0
395	2026-09-04	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	14	0	0	1	0
396	2026-09-07	view	http://130.89.182.51/	\N	\N	\N	1	1	330	1	0	0	0
397	2026-09-07	view	http://130.89.182.51:8000/	\N	\N	\N	3	3	199	3	0	0	0
398	2026-09-07	view	http://130.89.182.51:8000/actuator/configprops	\N	\N	\N	2	2	201	0	0	2	0
399	2026-09-07	view	http://130.89.182.51:8000/actuator/env	\N	\N	\N	2	2	193	0	0	2	0
400	2026-09-07	view	http://130.89.182.51:8000/actuator/health	\N	\N	\N	1	1	32	0	0	1	0
401	2026-09-07	view	http://130.89.182.51:8000/actuator/heapdump	\N	\N	\N	1	1	36	0	0	1	0
402	2026-09-07	view	http://130.89.182.51:8000/admin/	\N	\N	\N	1	1	13	0	0	1	0
403	2026-09-07	view	http://130.89.182.51:8000/admin/phpinfo.php	\N	\N	\N	1	1	113	0	0	1	0
404	2026-09-07	view	http://130.89.182.51:8000/Admin/phpinfo.php	\N	\N	\N	1	1	140	0	0	1	0
405	2026-09-07	view	http://130.89.182.51:8000/ai_config.json	\N	\N	\N	1	1	39	0	0	1	0
406	2026-09-07	view	http://130.89.182.51:8000/.aider.conf.yml	\N	\N	\N	1	1	9	0	0	1	0
407	2026-09-07	view	http://130.89.182.51:8000/.aider.env	\N	\N	\N	1	1	8	0	0	1	0
408	2026-09-07	view	http://130.89.182.51:8000/.aider.model.settings.yml	\N	\N	\N	1	1	8	0	0	1	0
409	2026-09-07	view	http://130.89.182.51:8000/ai.json	\N	\N	\N	1	1	51	0	0	1	0
410	2026-09-07	view	http://130.89.182.51:8000/.anthropic	\N	\N	\N	1	1	20	0	0	1	0
411	2026-09-07	view	http://130.89.182.51:8000/anthropic.json	\N	\N	\N	1	1	40	0	0	1	0
412	2026-09-07	view	http://130.89.182.51:8000/api/	\N	\N	\N	1	1	42	0	0	1	0
413	2026-09-07	view	http://130.89.182.51:8000/api/aws.json	\N	\N	\N	1	1	135	0	0	1	0
414	2026-09-07	view	http://130.89.182.51:8000/api/config.json	\N	\N	\N	1	1	92	0	0	1	0
415	2026-09-07	view	http://130.89.182.51:8000/api-docs	\N	\N	\N	1	1	39	0	0	1	0
416	2026-09-07	view	http://130.89.182.51:8000/api_keys.json	\N	\N	\N	1	1	99	0	0	1	0
417	2026-09-07	view	http://130.89.182.51:8000/api/phpinfo.php	\N	\N	\N	1	1	129	0	0	1	0
418	2026-09-07	view	http://130.89.182.51:8000/appsettings.json	\N	\N	\N	1	1	84	0	0	1	0
419	2026-09-07	view	http://130.89.182.51:8000/archive.zip	\N	\N	\N	1	1	11	0	0	1	0
420	2026-09-07	view	http://130.89.182.51:8000/auth.json	\N	\N	\N	1	1	126	0	0	1	0
421	2026-09-07	view	http://130.89.182.51:8000/.auth.json	\N	\N	\N	1	1	109	0	0	1	0
422	2026-09-07	view	http://130.89.182.51:8000/.aws/credentials	\N	\N	\N	1	1	20	0	0	1	0
423	2026-09-07	view	http://130.89.182.51:8000/backup.7z	\N	\N	\N	1	1	10	0	0	1	0
424	2026-09-07	view	http://130.89.182.51:8000/backup.rar	\N	\N	\N	1	1	10	0	0	1	0
425	2026-09-07	view	http://130.89.182.51:8000/backup.sql	\N	\N	\N	2	2	48	0	0	2	0
426	2026-09-07	view	http://130.89.182.51:8000/backup.tar	\N	\N	\N	1	1	21	0	0	1	0
427	2026-09-07	view	http://130.89.182.51:8000/backup.tar.gz	\N	\N	\N	1	1	36	0	0	1	0
428	2026-09-07	view	http://130.89.182.51:8000/backup.tgz	\N	\N	\N	1	1	21	0	0	1	0
429	2026-09-07	view	http://130.89.182.51:8000/backup.zip	\N	\N	\N	1	1	38	0	0	1	0
430	2026-09-07	view	http://130.89.182.51:8000/.bash_profile	\N	\N	\N	1	1	81	0	0	1	0
431	2026-09-07	view	http://130.89.182.51:8000/.bashrc	\N	\N	\N	1	1	47	0	0	1	0
432	2026-09-07	view	http://130.89.182.51:8000/.claude/.credentials.json	\N	\N	\N	1	1	10	0	0	1	0
433	2026-09-07	view	http://130.89.182.51:8000/claude_desktop_config.json	\N	\N	\N	1	1	3	0	0	1	0
434	2026-09-07	view	http://130.89.182.51:8000/.claude.json	\N	\N	\N	1	1	11	0	0	1	0
435	2026-09-07	view	http://130.89.182.51:8000/.claude/mcp.json	\N	\N	\N	1	1	8	0	0	1	0
436	2026-09-07	view	http://130.89.182.51:8000/.claude/settings.json	\N	\N	\N	1	1	10	0	0	1	0
437	2026-09-07	view	http://130.89.182.51:8000/.codeium/settings.json	\N	\N	\N	1	1	7	0	0	1	0
438	2026-09-07	view	http://130.89.182.51:8000/.codeium/windsurf/mcp_config.json	\N	\N	\N	1	1	9	0	0	1	0
439	2026-09-07	view	http://130.89.182.51:8000/composer.json	\N	\N	\N	1	1	38	0	0	1	0
440	2026-09-07	view	http://130.89.182.51:8000/.config/fish/config.fish	\N	\N	\N	1	1	199	0	0	1	0
441	2026-09-07	view	http://130.89.182.51:8000/config.js	\N	\N	\N	2	2	121	0	0	2	0
442	2026-09-07	view	http://130.89.182.51:8000/config.json	\N	\N	\N	2	2	117	0	0	2	0
443	2026-09-07	view	http://130.89.182.51:8000/config.php	\N	\N	\N	1	1	11	0	0	1	0
444	2026-09-07	view	http://130.89.182.51:8000/config.yaml	\N	\N	\N	1	1	28	0	0	1	0
445	2026-09-07	view	http://130.89.182.51:8000/config.yml	\N	\N	\N	1	1	27	0	0	1	0
446	2026-09-07	view	http://130.89.182.51:8000/.continue/config.json	\N	\N	\N	1	1	8	0	0	1	0
447	2026-09-07	view	http://130.89.182.51:8000/.continue/config.ts	\N	\N	\N	1	1	8	0	0	1	0
448	2026-09-07	view	http://130.89.182.51:8000/.continue/config.yaml	\N	\N	\N	1	1	9	0	0	1	0
449	2026-09-07	view	http://130.89.182.51:8000/credentials.json	\N	\N	\N	1	1	125	0	0	1	0
450	2026-09-07	view	http://130.89.182.51:8000/crusader-404-probe	\N	\N	\N	1	1	192	0	0	1	0
451	2026-09-07	view	http://130.89.182.51:8000/.cursor/config.json	\N	\N	\N	1	1	8	0	0	1	0
452	2026-09-07	view	http://130.89.182.51:8000/.cursor/mcp.json	\N	\N	\N	1	1	3	0	0	1	0
453	2026-09-07	view	http://130.89.182.51:8000/.cursorrules	\N	\N	\N	1	1	8	0	0	1	0
454	2026-09-07	view	http://130.89.182.51:8000/.cursor/settings.json	\N	\N	\N	1	1	8	0	0	1	0
455	2026-09-07	view	http://130.89.182.51:8000/database.sql	\N	\N	\N	2	2	43	0	0	2	0
456	2026-09-07	view	http://130.89.182.51:8000/data.sql	\N	\N	\N	1	1	11	0	0	1	0
457	2026-09-07	view	http://130.89.182.51:8000/db.sql	\N	\N	\N	2	2	50	0	0	2	0
458	2026-09-07	view	http://130.89.182.51:8000/debug	\N	\N	\N	1	1	34	0	0	1	0
459	2026-09-07	view	http://130.89.182.51:8000/.docker/config.json	\N	\N	\N	1	1	34	0	0	1	0
460	2026-09-07	view	http://130.89.182.51:8000/.DS_Store	\N	\N	\N	1	1	61	0	0	1	0
461	2026-09-07	view	http://130.89.182.51:8000/dump.sql	\N	\N	\N	2	2	52	0	0	2	0
462	2026-09-07	view	http://130.89.182.51:8000/dump.tar.gz	\N	\N	\N	1	1	15	0	0	1	0
463	2026-09-07	view	http://130.89.182.51:8000/env	\N	\N	\N	1	1	154	0	0	1	0
464	2026-09-07	view	http://130.89.182.51:8000/.env	\N	\N	\N	2	2	212	0	0	2	0
465	2026-09-07	view	http://130.89.182.51:8000/.env.backup	\N	\N	\N	1	1	151	0	0	1	0
466	2026-09-07	view	http://130.89.182.51:8000/.env.bak	\N	\N	\N	2	2	234	0	0	2	0
467	2026-09-07	view	http://130.89.182.51:8000/.env.dev	\N	\N	\N	1	1	142	0	0	1	0
468	2026-09-07	view	http://130.89.182.51:8000/.env.example	\N	\N	\N	2	2	232	0	0	2	0
469	2026-09-07	view	http://130.89.182.51:8000/.env.local	\N	\N	\N	2	2	163	0	0	2	0
470	2026-09-07	view	http://130.89.182.51:8000/.env.old	\N	\N	\N	1	1	163	0	0	1	0
471	2026-09-07	view	http://130.89.182.51:8000/.env.prod	\N	\N	\N	1	1	162	0	0	1	0
472	2026-09-07	view	http://130.89.182.51:8000/.env.production	\N	\N	\N	2	2	198	0	0	2	0
473	2026-09-07	view	http://130.89.182.51:8000/.env.save	\N	\N	\N	1	1	157	0	0	1	0
474	2026-09-07	view	http://130.89.182.51:8000/firebase-config.json	\N	\N	\N	1	1	74	0	0	1	0
475	2026-09-07	view	http://130.89.182.51:8000/firebase.json	\N	\N	\N	1	1	85	0	0	1	0
476	2026-09-07	view	http://130.89.182.51:8000/.fish_history	\N	\N	\N	1	1	117	0	0	1	0
477	2026-09-07	view	http://130.89.182.51:8000/gcp.json	\N	\N	\N	1	1	113	0	0	1	0
478	2026-09-07	view	http://130.89.182.51:8000/Gemfile	\N	\N	\N	1	1	38	0	0	1	0
479	2026-09-07	view	http://130.89.182.51:8000/.git/config	\N	\N	\N	1	1	16	0	0	1	0
480	2026-09-07	view	http://130.89.182.51:8000/.git/HEAD	\N	\N	\N	1	1	16	0	0	1	0
481	2026-09-07	view	http://130.89.182.51:8000/google.json	\N	\N	\N	1	1	108	0	0	1	0
482	2026-09-07	view	http://130.89.182.51:8000/graphiql	\N	\N	\N	1	1	34	0	0	1	0
483	2026-09-07	view	http://130.89.182.51:8000/graphql	\N	\N	\N	1	1	31	0	0	1	0
484	2026-09-07	view	http://130.89.182.51:8000/.htaccess	\N	\N	\N	1	1	144	0	0	1	0
485	2026-09-07	view	http://130.89.182.51:8000/htdocs.zip	\N	\N	\N	1	1	11	0	0	1	0
486	2026-09-07	view	http://130.89.182.51:8000/_ignition/health-check	\N	\N	\N	1	1	182	0	0	1	0
487	2026-09-07	view	http://130.89.182.51:8000/info.php	\N	\N	\N	1	1	134	0	0	1	0
488	2026-09-07	view	http://130.89.182.51:8000/keys.json	\N	\N	\N	1	1	160	0	0	1	0
489	2026-09-07	view	http://130.89.182.51:8000/litellm_config.yaml	\N	\N	\N	1	1	15	0	0	1	0
490	2026-09-07	view	http://130.89.182.51:8000/.litellm/config.yaml	\N	\N	\N	1	1	7	0	0	1	0
491	2026-09-07	view	http://130.89.182.51:8000/llm_config.json	\N	\N	\N	1	1	25	0	0	1	0
492	2026-09-07	view	http://130.89.182.51:8000/llm.json	\N	\N	\N	1	1	38	0	0	1	0
493	2026-09-07	view	http://130.89.182.51:8000/login	\N	\N	\N	1	1	11	0	0	1	0
494	2026-09-07	view	http://130.89.182.51:8000/mcp_config.json	\N	\N	\N	1	1	8	0	0	1	0
495	2026-09-07	view	http://130.89.182.51:8000/mcp.json	\N	\N	\N	1	1	9	0	0	1	0
496	2026-09-07	view	http://130.89.182.51:8000/.mcp.json	\N	\N	\N	1	1	4	0	0	1	0
497	2026-09-07	view	http://130.89.182.51:8000/.mysql_history	\N	\N	\N	1	1	135	0	0	1	0
498	2026-09-07	view	http://130.89.182.51:8000/mysql.sql	\N	\N	\N	1	1	11	0	0	1	0
499	2026-09-07	view	http://130.89.182.51:8000/.node_repl_history	\N	\N	\N	1	1	87	0	0	1	0
500	2026-09-07	view	http://130.89.182.51:8000/.openai	\N	\N	\N	1	1	21	0	0	1	0
501	2026-09-07	view	http://130.89.182.51:8000/openai.json	\N	\N	\N	1	1	48	0	0	1	0
502	2026-09-07	view	http://130.89.182.51:8000/openapi.json	\N	\N	\N	1	1	42	0	0	1	0
503	2026-09-07	view	http://130.89.182.51:8000/package.json	\N	\N	\N	1	1	37	0	0	1	0
504	2026-09-07	view	http://130.89.182.51:8000/phpinfo.php	\N	\N	\N	2	2	145	0	0	2	0
505	2026-09-07	view	http://130.89.182.51:8000/.profile	\N	\N	\N	1	1	87	0	0	1	0
506	2026-09-07	view	http://130.89.182.51:8000/proxy_config.yaml	\N	\N	\N	1	1	13	0	0	1	0
507	2026-09-07	view	http://130.89.182.51:8000/.psql_history	\N	\N	\N	1	1	156	0	0	1	0
508	2026-09-07	view	http://130.89.182.51:8000/public_html.zip	\N	\N	\N	1	1	12	0	0	1	0
509	2026-09-07	view	http://130.89.182.51:8000/.python_history	\N	\N	\N	1	1	141	0	0	1	0
510	2026-09-07	view	http://130.89.182.51:8000/requirements.txt	\N	\N	\N	1	1	33	0	0	1	0
511	2026-09-07	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	10	0	0	1	0
512	2026-09-07	view	http://130.89.182.51:8000/.secrets	\N	\N	\N	1	1	199	0	0	1	0
513	2026-09-07	view	http://130.89.182.51:8000/secrets.json	\N	\N	\N	1	1	105	0	0	1	0
514	2026-09-07	view	http://130.89.182.51:8000/.secrets.json	\N	\N	\N	1	1	159	0	0	1	0
515	2026-09-07	view	http://130.89.182.51:8000/secrets.yaml	\N	\N	\N	1	1	158	0	0	1	0
516	2026-09-07	view	http://130.89.182.51:8000/secrets.yml	\N	\N	\N	1	1	138	0	0	1	0
517	2026-09-07	view	http://130.89.182.51:8000/server.key	\N	\N	\N	1	1	36	0	0	1	0
518	2026-09-07	view	http://130.89.182.51:8000/server-status	\N	\N	\N	1	1	14	0	0	1	0
519	2026-09-07	view	http://130.89.182.51:8000/service-account.json	\N	\N	\N	1	1	72	0	0	1	0
520	2026-09-07	view	http://130.89.182.51:8000/service_account.json	\N	\N	\N	1	1	144	0	0	1	0
521	2026-09-07	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	21	0	0	1	0
522	2026-09-07	view	http://130.89.182.51:8000/site.zip	\N	\N	\N	1	1	12	0	0	1	0
523	2026-09-07	view	http://130.89.182.51:8000/sql.gz	\N	\N	\N	1	1	9	0	0	1	0
524	2026-09-07	view	http://130.89.182.51:8000/storage/logs/laravel.log	\N	\N	\N	1	1	169	0	0	1	0
525	2026-09-07	view	http://130.89.182.51:8000/swagger.json	\N	\N	\N	1	1	43	0	0	1	0
526	2026-09-07	view	http://130.89.182.51:8000/swagger/v1/swagger.json	\N	\N	\N	1	1	41	0	0	1	0
527	2026-09-07	view	http://130.89.182.51:8000/tokens.json	\N	\N	\N	1	1	152	0	0	1	0
528	2026-09-07	view	http://130.89.182.51:8000/.vscode/mcp.json	\N	\N	\N	1	1	10	0	0	1	0
529	2026-09-07	view	http://130.89.182.51:8000/web.zip	\N	\N	\N	1	1	11	0	0	1	0
530	2026-09-07	view	http://130.89.182.51:8000/.well-known/change-password	\N	\N	\N	1	1	33	0	0	1	0
531	2026-09-07	view	http://130.89.182.51:8000/.well-known/security.txt	\N	\N	\N	1	1	19	0	0	1	0
532	2026-09-07	view	http://130.89.182.51:8000/.windsurf/mcp.json	\N	\N	\N	1	1	11	0	0	1	0
533	2026-09-07	view	http://130.89.182.51:8000/wp-config.php~	\N	\N	\N	1	1	183	0	0	1	0
534	2026-09-07	view	http://130.89.182.51:8000/wp-config.php.bak	\N	\N	\N	1	1	189	0	0	1	0
535	2026-09-07	view	http://130.89.182.51:8000/wp-config.php.swp	\N	\N	\N	1	1	195	0	0	1	0
536	2026-09-07	view	http://130.89.182.51:8000/wp-login.php	\N	\N	\N	1	1	30	0	0	1	0
537	2026-09-07	view	http://130.89.182.51:8000/www.zip	\N	\N	\N	1	1	16	0	0	1	0
538	2026-09-07	view	http://130.89.182.51:8000/.zprofile	\N	\N	\N	1	1	136	0	0	1	0
539	2026-09-07	view	http://130.89.182.51:8000/.zsh_history	\N	\N	\N	1	1	46	0	0	1	0
540	2026-09-07	view	http://130.89.182.51:8000/.zshrc	\N	\N	\N	1	1	39	0	0	1	0
541	2026-09-08	view	http://130.89.182.51/	\N	\N	\N	1	1	73	1	0	0	0
542	2026-09-08	view	http://130.89.182.51:8000/	\N	\N	\N	14	10	624	13	0	1	0
543	2026-09-08	view	http://130.89.182.51:8000/ads.txt	\N	\N	\N	1	1	27	0	0	1	0
544	2026-09-08	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	6	0	0	1	0
545	2026-09-08	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	6	6	64	0	0	6	0
546	2026-09-08	view	http://130.89.182.51:8000/login	\N	\N	\N	1	1	18	0	0	1	0
547	2026-09-08	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	3	0	0	1	0
548	2026-09-08	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	4	0	0	1	0
549	2026-09-08	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	5	0	0	1	0
550	2026-09-08	view	http://130.89.182.51:8000/server_info	\N	\N	\N	1	1	60	0	0	1	0
551	2026-09-08	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	5	0	0	1	0
552	2026-09-08	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
553	2026-09-08	view	http://130.89.182.51/favicon.ico	\N	\N	\N	1	1	10	0	0	1	0
554	2026-09-08	view	http://localhost:8000/api/fer-types	\N	\N	\N	1	1	11	0	0	1	0
555	2026-09-08	view	http://localhost:8000/api/health	\N	\N	\N	32	1	392	0	0	32	0
556	2026-09-09	sparql_query	\N	\N	\N	\N	2	2	193	2	0	0	0
557	2026-09-09	view	http://130.89.182.51/	\N	\N	\N	2	1	131	1	0	1	0
558	2026-09-09	view	http://130.89.182.51:8000/	\N	\N	\N	4	3	218	4	0	0	0
559	2026-09-09	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	43	0	0	1	0
560	2026-09-09	view	http://api.ipify.org/	\N	\N	\N	1	1	32	1	0	0	0
561	2026-09-09	view	http://localhost:8000/	\N	\N	\N	4	2	263	4	0	0	0
562	2026-09-09	view	http://localhost:8000/fdp-api/info	\N	\N	\N	6	2	27	6	0	0	0
563	2026-09-09	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	2	2	182	2	0	0	0
564	2026-09-09	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	6	2	966	6	0	0	0
565	2026-09-09	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	6	2	1092	6	0	0	0
566	2026-09-10	view	http://130.89.182.51:8000/	\N	\N	\N	2	2	334	2	0	0	0
567	2026-09-10	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	10	0	0	1	0
568	2026-09-10	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	2	2	72	0	0	2	0
569	2026-09-10	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	17	0	0	1	0
570	2026-09-10	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	2	2	34	0	0	2	0
571	2026-09-10	view	http://130.89.182.51:8000/wp-content/uploads/wp-cache-flush.php	\N	\N	\N	1	1	404	0	0	1	0
572	2026-09-11	view	http://localhost:8000/	\N	\N	\N	1	1	388	1	0	0	0
573	2026-09-11	view	http://localhost:8000/api/health	\N	\N	\N	2	1	62	0	0	2	0
574	2026-09-13	view	http://localhost:8000/	\N	\N	\N	2	2	573	2	0	0	0
575	2026-09-14	view	http://130.89.182.51:8000/	\N	\N	\N	2	2	657	2	0	0	0
576	2026-09-14	view	http://130.89.182.51:8000/actuator/configprops	\N	\N	\N	1	1	3	0	0	1	0
577	2026-09-14	view	http://130.89.182.51:8000/actuator/env	\N	\N	\N	1	1	6	0	0	1	0
578	2026-09-14	view	http://130.89.182.51:8000/api/	\N	\N	\N	1	1	6	0	0	1	0
579	2026-09-14	view	http://130.89.182.51:8000/api-docs	\N	\N	\N	1	1	2	0	0	1	0
580	2026-09-14	view	http://130.89.182.51:8000/api/openapi.json	\N	\N	\N	1	1	14	0	0	1	0
581	2026-09-14	view	http://130.89.182.51:8000/application.properties	\N	\N	\N	1	1	27	0	0	1	0
582	2026-09-14	view	http://130.89.182.51:8000/application.yaml	\N	\N	\N	1	1	22	0	0	1	0
583	2026-09-14	view	http://130.89.182.51:8000/application.yml	\N	\N	\N	1	1	33	0	0	1	0
584	2026-09-14	view	http://130.89.182.51:8000/appsettings.json	\N	\N	\N	1	1	6	0	0	1	0
585	2026-09-14	view	http://130.89.182.51:8000/appsettings.Production.json	\N	\N	\N	1	1	29	0	0	1	0
586	2026-09-14	view	http://130.89.182.51:8000/.aws/credentials	\N	\N	\N	1	1	11	0	0	1	0
587	2026-09-14	view	http://130.89.182.51:8000/composer.json	\N	\N	\N	1	1	4	0	0	1	0
588	2026-09-14	view	http://130.89.182.51:8000/config.js	\N	\N	\N	1	1	16	0	0	1	0
589	2026-09-14	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	21	0	0	1	0
590	2026-09-14	view	http://130.89.182.51:8000/config.php	\N	\N	\N	1	1	60	0	0	1	0
591	2026-09-14	view	http://130.89.182.51:8000/config.php.bak	\N	\N	\N	1	1	92	0	0	1	0
592	2026-09-14	view	http://130.89.182.51:8000/config/secrets.yml	\N	\N	\N	1	1	22	0	0	1	0
593	2026-09-14	view	http://130.89.182.51:8000/config.yaml	\N	\N	\N	1	1	18	0	0	1	0
594	2026-09-14	view	http://130.89.182.51:8000/config.yml	\N	\N	\N	1	1	16	0	0	1	0
595	2026-09-14	view	http://130.89.182.51:8000/conf/tomcat-users.xml	\N	\N	\N	1	1	23	0	0	1	0
596	2026-09-14	view	http://130.89.182.51:8000/debug	\N	\N	\N	1	1	12	0	0	1	0
597	2026-09-14	view	http://130.89.182.51:8000/.docker/config.json	\N	\N	\N	1	1	8	0	0	1	0
598	2026-09-14	view	http://130.89.182.51:8000/docs	\N	\N	\N	1	1	17	0	0	1	0
599	2026-09-14	view	http://130.89.182.51:8000/.env	\N	\N	\N	1	1	14	0	0	1	0
600	2026-09-14	view	http://130.89.182.51:8000/.env.bak	\N	\N	\N	1	1	56	0	0	1	0
601	2026-09-14	view	http://130.89.182.51:8000/env.js	\N	\N	\N	1	1	11	0	0	1	0
602	2026-09-14	view	http://130.89.182.51:8000/.env.local	\N	\N	\N	1	1	34	0	0	1	0
603	2026-09-14	view	http://130.89.182.51:8000/.env.production	\N	\N	\N	1	1	27	0	0	1	0
604	2026-09-14	view	http://130.89.182.51:8000/.env.production.local	\N	\N	\N	1	1	14	0	0	1	0
605	2026-09-14	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	59	0	0	1	0
606	2026-09-14	view	http://130.89.182.51:8000/.git/config	\N	\N	\N	1	1	58	0	0	1	0
607	2026-09-14	view	http://130.89.182.51:8000/.git-credentials	\N	\N	\N	1	1	24	0	0	1	0
608	2026-09-14	view	http://130.89.182.51:8000/.kube/config	\N	\N	\N	1	1	17	0	0	1	0
609	2026-09-14	view	http://130.89.182.51:8000/.npmrc	\N	\N	\N	1	1	27	0	0	1	0
610	2026-09-14	view	http://130.89.182.51:8000/openapi.json	\N	\N	\N	1	1	5	0	0	1	0
611	2026-09-14	view	http://130.89.182.51:8000/openapi.yaml	\N	\N	\N	1	1	21	0	0	1	0
612	2026-09-14	view	http://130.89.182.51:8000/openapi.yml	\N	\N	\N	1	1	21	0	0	1	0
613	2026-09-14	view	http://130.89.182.51:8000/package.json	\N	\N	\N	1	1	3	0	0	1	0
614	2026-09-14	view	http://130.89.182.51:8000/phpinfo.php	\N	\N	\N	1	1	22	0	0	1	0
615	2026-09-14	view	http://130.89.182.51:8000/_profiler	\N	\N	\N	1	1	20	0	0	1	0
616	2026-09-14	view	http://130.89.182.51:8000/.pypirc	\N	\N	\N	1	1	33	0	0	1	0
617	2026-09-14	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	6	0	0	1	0
618	2026-09-14	view	http://130.89.182.51:8000/runtime-config.js	\N	\N	\N	1	1	22	0	0	1	0
619	2026-09-14	view	http://130.89.182.51:8000/server.key	\N	\N	\N	1	1	8	0	0	1	0
620	2026-09-14	view	http://130.89.182.51:8000/sitemap_index.xml	\N	\N	\N	1	1	15	0	0	1	0
621	2026-09-14	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	6	0	0	1	0
622	2026-09-14	view	http://130.89.182.51:8000/swagger.json	\N	\N	\N	1	1	4	0	0	1	0
623	2026-09-14	view	http://130.89.182.51:8000/swagger/v1/swagger.json	\N	\N	\N	1	1	7	0	0	1	0
624	2026-09-14	view	http://130.89.182.51:8000/telescope	\N	\N	\N	1	1	15	0	0	1	0
625	2026-09-14	view	http://130.89.182.51:8000/v2/api-docs	\N	\N	\N	1	1	8	0	0	1	0
626	2026-09-14	view	http://130.89.182.51:8000/v3/api-docs	\N	\N	\N	1	1	13	0	0	1	0
627	2026-09-14	view	http://api.ipify.org/	\N	\N	\N	1	1	63	1	0	0	0
628	2026-09-28	view	http://130.89.182.51:8000/	\N	\N	\N	3	3	708	3	0	0	0
629	2026-09-28	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	16	0	0	1	0
630	2026-09-28	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	15	0	0	1	0
631	2026-09-28	view	http://localhost:8000/apple-touch-icon.png	\N	\N	\N	2	1	1205	0	0	2	0
632	2026-09-28	view	http://localhost:8000/apple-touch-icon-precomposed.png	\N	\N	\N	2	1	2289	0	0	2	0
633	2026-09-28	view	http://localhost:8000/favicon.ico	\N	\N	\N	2	1	1191	0	0	2	0
634	2026-09-29	view	http://130.89.182.51:8000/	\N	\N	\N	6	4	860	6	0	0	0
635	2026-09-29	view	http://130.89.182.51:8000/api/v2/heartbeat	\N	\N	\N	1	0	109	0	0	1	0
636	2026-09-29	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	3	2	107	0	0	3	0
637	2026-09-30	view	http://130.89.182.51:8000/	\N	\N	\N	11	10	508	10	0	1	0
638	2026-09-30	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	7	0	0	1	0
639	2026-09-30	view	http://130.89.182.51:8000/config.json	\N	\N	\N	1	1	9	0	0	1	0
640	2026-09-30	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	5	5	48	0	0	5	0
641	2026-09-30	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	27	0	0	1	0
642	2026-09-30	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	9	0	0	1	0
643	2026-09-30	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	8	0	0	1	0
644	2026-09-30	view	http://130.89.182.51:8000/sitemap.xml	\N	\N	\N	1	1	13	0	0	1	0
645	2026-09-30	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
646	2026-09-30	view	http://172.18.0.3:8000/	\N	\N	\N	1	0	56	1	0	0	0
647	2026-09-30	view	http://172.18.0.3:8000/nice ports,/Trinity.txt.bak	\N	\N	\N	1	0	96	0	0	0	1
648	2026-09-30	view	http://api.ipify.org/	\N	\N	\N	1	1	70	1	0	0	0
649	2026-10-26	view	http://130.89.182.51:8000/	\N	\N	\N	8	7	889	8	0	0	0
650	2026-10-26	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	2	2	66	0	0	2	0
651	2026-10-26	view	http://130.89.182.51:8000/security.txt	\N	\N	\N	1	1	14	0	0	1	0
\.


--
-- Data for Name: metrics_hourly; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.metrics_hourly (id, bucket, event_type, resource_iri, country_code, region, city, request_count, unique_visitors, latency_ms_sum, status_2xx_count, status_3xx_count, status_4xx_count, status_5xx_count) FROM stdin;
900	2026-10-28 11:00:00+00	view	http://localhost:8000/	\N	\N	\N	1	1	72	1	0	0	0
901	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/config	\N	\N	\N	1	1	3	1	0	0	0
902	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/info	\N	\N	\N	1	1	5	1	0	0	0
1347	2026-10-28 13:00:00+00	modify	http://localhost:8000/catalog/environmental-monitoring	\N	\N	\N	2	2	217	2	0	0	0
1356	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/policies/curated-catalog	\N	\N	\N	5	2	46	2	0	3	0
1383	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/policies/curated-catalog/state	\N	\N	\N	1	1	34	1	0	0	0
1088	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/catalog/soil-and-groundwater-catalog/state	\N	\N	\N	6	5	275	6	0	0	0
1087	2026-10-28 13:00:00+00	modify	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	\N	\N	\N	5	5	468	5	0	0	0
1094	2026-10-28 13:00:00+00	view	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	\N	\N	\N	19	7	434	17	0	2	0
1095	2026-10-28 13:00:00+00	view	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024/meta	\N	\N	\N	9	6	299	9	0	0	0
1097	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/dataset/groundwater-levels-twente-2020-to-2024/expanded	\N	\N	\N	9	6	702	9	0	0	0
903	2026-10-28 11:00:00+00	modify	http://localhost:8000/catalog	\N	\N	\N	5	1	241	2	0	3	0
905	2026-10-28 11:00:00+00	modify	http://localhost:8000/distribution	\N	\N	\N	6	1	378	6	0	0	0
906	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/licenses/cc0-1.0	\N	\N	\N	4	1	128	4	0	0	0
907	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/licenses/cc-by-4.0	\N	\N	\N	4	1	357	4	0	0	0
908	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	\N	\N	\N	4	1	126	4	0	0	0
909	2026-10-28 11:00:00+00	view	http://localhost:8000/catalog/clinical-research	\N	\N	\N	1	1	7	0	0	1	0
912	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/biobank-sample-catalog	\N	\N	\N	1	1	5	0	0	1	0
913	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/rare-disease-registry	\N	\N	\N	1	1	5	0	0	1	0
914	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/river-water-temperature	\N	\N	\N	1	1	8	0	0	1	0
915	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/urban-tree-inventory	\N	\N	\N	1	1	6	0	0	1	0
916	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/air-quality-2024-csv	\N	\N	\N	1	1	5	0	0	1	0
917	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/air-quality-2024-stations	\N	\N	\N	1	1	5	0	0	1	0
918	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/biobank-access-request	\N	\N	\N	1	1	7	0	0	1	0
919	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/river-temperature-api	\N	\N	\N	1	1	5	0	0	1	0
920	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/river-temperature-csv	\N	\N	\N	1	1	6	0	0	1	0
921	2026-10-28 11:00:00+00	view	http://localhost:8000/distribution/urban-trees-geojson	\N	\N	\N	1	1	5	0	0	1	0
1120	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/dataset/groundwater-levels-twente-2020-to-2024/state	\N	\N	\N	2	2	97	2	0	0	0
1392	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/index/targets	\N	\N	\N	1	1	10	1	0	0	0
1395	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/settings	\N	\N	\N	1	1	16	1	0	0	0
1396	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/users	\N	\N	\N	6	1	231	6	0	0	0
910	2026-10-28 11:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring	\N	\N	\N	8	4	239	4	0	4	0
911	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/air-quality-enschede-2024	\N	\N	\N	9	5	361	8	0	1	0
904	2026-10-28 11:00:00+00	modify	http://localhost:8000/dataset	\N	\N	\N	6	2	445	6	0	0	0
1397	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/users/roles	\N	\N	\N	1	1	2	1	0	0	0
1398	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/me/api-keys	\N	\N	\N	1	1	10	1	0	0	0
1401	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/me/api-keys	\N	\N	\N	2	1	9	2	0	0	0
1404	2026-10-28 14:00:00+00	delete	http://localhost:8000/fdp-api/me/api-keys/25a4bac6-c632-45bb-bacc-7fd0e8d29ade	\N	\N	\N	1	1	4	1	0	0	0
1405	2026-10-28 14:00:00+00	view	http://localhost:8000/fdp-api/me/api-keys	\N	\N	\N	1	1	22	1	0	0	0
1408	2026-10-29 13:00:00+00	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	1	1	222	1	0	0	0
1406	2026-10-29 13:00:00+00	sparql_query	\N	\N	\N	\N	3	3	694	3	0	0	0
1407	2026-10-29 13:00:00+00	view	http://localhost:8000	\N	\N	\N	5	3	992	5	0	0	0
1409	2026-10-29 13:00:00+00	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	15	5	810	15	0	0	0
1410	2026-10-29 13:00:00+00	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	15	5	1371	15	0	0	0
1438	2026-10-29 16:00:00+00	sparql_query	\N	\N	\N	\N	2	1	71	2	0	0	0
1166	2026-10-28 13:00:00+00	delete	http://localhost:8000/catalog/soil-and-groundwater-catalog	\N	\N	\N	1	1	105	1	0	0	0
1167	2026-10-28 13:00:00+00	delete	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	\N	\N	\N	1	1	107	1	0	0	0
1168	2026-10-28 13:00:00+00	delete	http://localhost:8000/fdp-api/me/saved-queries/1a0662dc-e41c-4004-8811-e2b595b55031	\N	\N	\N	1	1	9	1	0	0	0
1419	2026-10-29 13:00:00+00	view	http://localhost:8000/fdp-api/users	\N	\N	\N	1	1	283	1	0	0	0
1424	2026-10-29 15:00:00+00	sparql_query	\N	\N	\N	\N	2	2	347	2	0	0	0
1425	2026-10-29 15:00:00+00	view	http://localhost:8000	\N	\N	\N	4	2	593	4	0	0	0
1426	2026-10-29 15:00:00+00	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	2	2	289	2	0	0	0
1427	2026-10-29 15:00:00+00	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	6	2	1113	6	0	0	0
1428	2026-10-29 15:00:00+00	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	6	2	1730	6	0	0	0
926	2026-10-28 11:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring/meta	\N	\N	\N	4	3	245	4	0	0	0
928	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/air-quality-enschede-2024/meta	\N	\N	\N	4	3	300	4	0	0	0
929	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/catalog/environmental-monitoring/expanded	\N	\N	\N	4	3	374	4	0	0	0
930	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/dataset/air-quality-enschede-2024/expanded	\N	\N	\N	4	3	421	4	0	0	0
931	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	8	3	211	8	0	0	0
933	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/policies/system-default	\N	\N	\N	6	3	31	6	0	0	0
922	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/search	\N	\N	\N	6	5	174	6	0	0	0
923	2026-10-28 11:00:00+00	sparql_query	\N	\N	\N	\N	90	16	6434	90	0	0	0
924	2026-10-28 11:00:00+00	view	http://localhost:8000	\N	\N	\N	43	13	2471	43	0	0	0
932	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	6	5	75	6	0	0	0
934	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	42	13	61	42	0	0	0
935	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	42	13	1822	42	0	0	0
974	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/me/saved-queries	\N	\N	\N	1	1	19	1	0	0	0
989	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/index/targets	\N	\N	\N	1	1	4	1	0	0	0
991	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/me/api-keys	\N	\N	\N	1	1	8	1	0	0	0
992	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/me/dashboard	\N	\N	\N	1	1	55	1	0	0	0
996	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/settings	\N	\N	\N	1	1	9	1	0	0	0
998	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/users/roles	\N	\N	\N	1	1	1	1	0	0	0
1176	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/me/saved-queries	\N	\N	\N	1	1	7	1	0	0	0
997	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/users	\N	\N	\N	4	3	306	1	0	0	3
1434	2026-10-29 16:00:00+00	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	6	2	359	6	0	0	0
978	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/me/saved-queries	\N	\N	\N	5	3	71	5	0	0	0
993	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	5	4	72	5	0	0	0
1435	2026-10-29 16:00:00+00	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	8	3	671	8	0	0	0
1012	2026-10-28 11:00:00+00	delete	http://localhost:8000/fdp-api/me/api-keys/f1554fb5-2159-4c7a-b398-90ebbeec10de	\N	\N	\N	1	1	9	1	0	0	0
1014	2026-10-28 11:00:00+00	modify	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	\N	\N	\N	1	1	52	1	0	0	0
1015	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/dataset/noise-levels-enschede-airport-2025/state	\N	\N	\N	1	1	31	1	0	0	0
1016	2026-10-28 11:00:00+00	modify	http://localhost:8000/fdp-api/me/api-keys	\N	\N	\N	1	1	24	1	0	0	0
1020	2026-10-28 11:00:00+00	view	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	\N	\N	\N	2	1	9	2	0	0	0
1242	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/schemas/dataset	\N	\N	\N	1	1	10	1	0	0	0
1007	2026-10-28 11:00:00+00	view	http://localhost:8000/fdp-api/catalog/spec	\N	\N	\N	4	3	18	4	0	0	0
1036	2026-10-28 12:00:00+00	sparql_query	\N	\N	\N	\N	4	1	241	4	0	0	0
1037	2026-10-28 12:00:00+00	view	http://localhost:8000	\N	\N	\N	2	1	117	2	0	0	0
1038	2026-10-28 12:00:00+00	view	http://localhost:8000/fdp-api/me/dashboard	\N	\N	\N	1	1	45	1	0	0	0
1039	2026-10-28 12:00:00+00	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	2	1	0	2	0	0	0
1040	2026-10-28 12:00:00+00	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	2	1	91	2	0	0	0
1307	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/policies/curated-catalog	\N	\N	\N	3	3	100	1	0	2	0
1308	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/policies/curated-catalog/validate	\N	\N	\N	3	3	32	3	0	0	0
1053	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/me/dashboard	\N	\N	\N	5	4	183	5	0	0	0
1249	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/dataset-catalog	\N	\N	\N	4	3	158	4	0	0	0
1250	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/dataset-catalog/meta	\N	\N	\N	3	3	121	3	0	0	0
1251	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/catalog/dataset-catalog/expanded	\N	\N	\N	3	3	169	3	0	0	0
1044	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/catalog/spec	\N	\N	\N	14	11	86	14	0	0	0
1045	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/licenses	\N	\N	\N	24	17	241	24	0	0	0
1041	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/search	\N	\N	\N	10	8	250	10	0	0	0
1046	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/me/saved-queries	\N	\N	\N	12	9	110	12	0	0	0
1047	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/policies	\N	\N	\N	29	20	559	29	0	0	0
1271	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/schemas/ontology	\N	\N	\N	1	1	106	1	0	0	0
1272	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/schemas/ontology/validate	\N	\N	\N	1	1	13	1	0	0	0
1042	2026-10-28 13:00:00+00	sparql_query	\N	\N	\N	\N	352	39	20434	352	0	0	0
1043	2026-10-28 13:00:00+00	view	http://localhost:8000	\N	\N	\N	150	39	8248	150	0	0	0
1048	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	145	43	176	145	0	0	0
1049	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	138	40	5501	138	0	0	0
1080	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/labels	\N	\N	\N	21	12	515	21	0	0	0
1079	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/dataset/spec	\N	\N	\N	5	5	31	5	0	0	0
1287	2026-10-28 13:00:00+00	delete	http://localhost:8000/fdp-api/resource-definitions/ontology	\N	\N	\N	1	1	119	1	0	0	0
1281	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	2	2	234	2	0	0	0
1084	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/policies/system-default	\N	\N	\N	22	14	188	22	0	0	0
1071	2026-10-28 13:00:00+00	modify	http://localhost:8000/catalog/soil-and-groundwater-catalog	\N	\N	\N	3	3	363	3	0	0	0
1075	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/soil-and-groundwater-catalog	\N	\N	\N	19	8	463	17	0	2	0
1076	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/soil-and-groundwater-catalog/meta	\N	\N	\N	10	7	362	10	0	0	0
1077	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/catalog/soil-and-groundwater-catalog/expanded	\N	\N	\N	10	7	608	10	0	0	0
1293	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/resource-definitions/catalog	\N	\N	\N	1	1	127	1	0	0	0
1326	2026-10-28 13:00:00+00	modify	http://localhost:8000/fdp-api/licenses/odc-by-1.0	\N	\N	\N	2	2	166	1	0	1	0
1338	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring/meta	\N	\N	\N	4	3	240	4	0	0	0
1339	2026-10-28 13:00:00+00	view	http://localhost:8000/fdp-api/catalog/environmental-monitoring/expanded	\N	\N	\N	4	3	418	4	0	0	0
1337	2026-10-28 13:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring	\N	\N	\N	8	5	294	8	0	0	0
823	2026-10-27 01:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	64	1	0	0	0
824	2026-10-27 04:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	2	1	65	2	0	0	0
825	2026-10-27 04:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	10	0	0	1	0
826	2026-10-27 04:00:00+00	view	http://130.89.182.51:8000/login	\N	\N	\N	1	1	12	0	0	1	0
827	2026-10-27 06:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	2	2	68	2	0	0	0
828	2026-10-27 06:00:00+00	view	http://130.89.182.51:8000/downloads/	\N	\N	\N	1	1	10	0	0	1	0
829	2026-10-27 06:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	8	0	0	1	0
830	2026-10-27 06:00:00+00	view	http://130.89.182.51:8000/files/	\N	\N	\N	1	1	21	0	0	1	0
831	2026-10-27 06:00:00+00	view	http://130.89.182.51:8000/pub/	\N	\N	\N	1	1	36	0	0	1	0
832	2026-10-27 07:00:00+00	view	http://130.89.182.51/	\N	\N	\N	1	1	68	1	0	0	0
833	2026-10-27 08:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	56	1	0	0	0
834	2026-10-27 08:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	10	0	0	1	0
835	2026-10-27 08:00:00+00	view	http://api.ipify.org/	\N	\N	\N	1	1	44	1	0	0	0
836	2026-10-27 14:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	126	1	0	0	0
837	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	2	1	55	1	0	1	0
838	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/api/mcp	\N	\N	\N	1	1	0	0	0	1	0
839	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	7	0	0	1	0
840	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	0	0	0	1	0
841	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/mcp/	\N	\N	\N	1	1	0	0	0	1	0
842	2026-10-27 15:00:00+00	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	1	0	0	1	0
843	2026-10-27 16:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	3	2	122	3	0	0	0
844	2026-10-27 16:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	7	0	0	1	0
845	2026-10-27 16:00:00+00	view	http://130.89.182.51:8000/robots.txt	\N	\N	\N	1	1	13	0	0	1	0
846	2026-10-27 18:00:00+00	view	http://130.89.182.51/	\N	\N	\N	1	1	52	1	0	0	0
847	2026-10-27 18:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	48	1	0	0	0
848	2026-10-27 18:00:00+00	view	http://130.89.182.51/favicon.ico	\N	\N	\N	1	1	11	0	0	1	0
849	2026-10-27 19:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	2	2	75	2	0	0	0
850	2026-10-27 20:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	0	47	1	0	0	0
851	2026-10-27 21:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	54	1	0	0	0
852	2026-10-27 22:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	15	0	0	1	0
853	2026-10-28 00:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	31	1	0	0	0
854	2026-10-28 00:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	14	0	0	1	0
855	2026-10-28 00:00:00+00	view	http://api.ipify.org/	\N	\N	\N	1	1	27	1	0	0	0
856	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	5	5	129	5	0	0	0
857	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/aj.html	\N	\N	\N	1	1	26	0	0	1	0
858	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/api/system	\N	\N	\N	1	1	27	0	0	1	0
859	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin/apcupsd/multimon.cgi	\N	\N	\N	1	1	38	0	0	1	0
860	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin-hax/ExportSettings.sh	\N	\N	\N	1	1	22	0	0	1	0
861	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin/multimon.cgi	\N	\N	\N	1	1	21	0	0	1	0
862	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin/nobody/Machine.cgi	\N	\N	\N	1	1	22	0	0	1	0
863	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin/privatekey.pem	\N	\N	\N	1	1	27	0	0	1	0
864	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/cgi-bin/user/Config.cgi	\N	\N	\N	2	2	33	0	0	2	0
865	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/config/cam_portal.cgi	\N	\N	\N	1	1	23	0	0	1	0
866	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/config/user.xml	\N	\N	\N	1	1	30	0	0	1	0
867	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/conf/nginx.conf	\N	\N	\N	1	1	11	0	0	1	0
868	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/current_config/passwd	\N	\N	\N	1	1	17	0	0	1	0
869	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/current_config/Sha1Account1	\N	\N	\N	1	1	34	0	0	1	0
870	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/install/	\N	\N	\N	1	1	17	0	0	1	0
871	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/mobile.html	\N	\N	\N	1	1	31	0	0	1	0
872	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/self.key	\N	\N	\N	1	1	37	0	0	1	0
873	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/service/v1/service-details	\N	\N	\N	1	1	27	0	0	1	0
874	2026-10-28 01:00:00+00	view	http://130.89.182.51:8000/status.html	\N	\N	\N	1	1	29	0	0	1	0
875	2026-10-28 02:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	52	1	0	0	0
876	2026-10-28 03:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	52	1	0	0	0
877	2026-10-28 04:00:00+00	modify	http://130.89.182.51:8000/mcp	\N	\N	\N	1	1	4	0	0	1	0
878	2026-10-28 04:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	1	1	54	1	0	0	0
879	2026-10-28 04:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	13	0	0	1	0
880	2026-10-28 04:00:00+00	view	http://130.89.182.51:8000/sse	\N	\N	\N	1	1	5	0	0	1	0
881	2026-10-28 05:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	3	3	186	3	0	0	0
882	2026-10-28 07:00:00+00	view	http://localhost:8000/apple-touch-icon.png	\N	\N	\N	2	1	98	0	0	2	0
883	2026-10-28 07:00:00+00	view	http://localhost:8000/apple-touch-icon-precomposed.png	\N	\N	\N	2	1	104	0	0	2	0
884	2026-10-28 07:00:00+00	view	http://localhost:8000/favicon.ico	\N	\N	\N	2	1	96	0	0	2	0
885	2026-10-28 08:00:00+00	view	http://130.89.182.51:8000/	\N	\N	\N	3	3	118	3	0	0	0
886	2026-10-28 08:00:00+00	view	http://130.89.182.51:8000/downloads/	\N	\N	\N	1	1	10	0	0	1	0
887	2026-10-28 08:00:00+00	view	http://130.89.182.51:8000/favicon.ico	\N	\N	\N	1	1	12	0	0	1	0
888	2026-10-28 08:00:00+00	view	http://130.89.182.51:8000/files/	\N	\N	\N	1	1	7	0	0	1	0
889	2026-10-28 08:00:00+00	view	http://130.89.182.51:8000/pub/	\N	\N	\N	1	1	10	0	0	1	0
890	2026-10-28 09:00:00+00	view	http://localhost:8000/fdp-api/config	\N	\N	\N	1	1	25	1	0	0	0
891	2026-10-28 09:00:00+00	view	http://localhost:8000/fdp-api/info	\N	\N	\N	1	1	22	1	0	0	0
892	2026-10-28 10:00:00+00	modify	http://localhost:8000/fdp-api/search	\N	\N	\N	1	1	42	1	0	0	0
893	2026-10-28 10:00:00+00	sparql_query	\N	\N	\N	\N	10	1	792	10	0	0	0
894	2026-10-28 10:00:00+00	view	http://localhost:8000/	\N	\N	\N	7	2	607	7	0	0	0
895	2026-10-28 10:00:00+00	view	http://localhost:8000/fdp-api/config	\N	\N	\N	5	1	11	5	0	0	0
896	2026-10-28 10:00:00+00	view	http://localhost:8000/fdp-api/expanded	\N	\N	\N	1	1	82	1	0	0	0
897	2026-10-28 10:00:00+00	view	http://localhost:8000/fdp-api/info	\N	\N	\N	5	1	0	5	0	0	0
898	2026-10-28 10:00:00+00	view	http://localhost:8000/fdp-api/resource-definitions	\N	\N	\N	5	1	5	5	0	0	0
899	2026-10-28 10:00:00+00	view	http://localhost:8000/fdp-api/schemas	\N	\N	\N	5	1	323	5	0	0	0
\.


--
-- Data for Name: metrics_raw; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.metrics_raw (id, bucket, event_type, resource_iri, country_code, region, city, visitor_hash, status_code, latency_ms, recorded_at) FROM stdin;
2823	2026-10-29 16:00:00+00	sparql_query	\N	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	25	2026-10-29 16:20:19.952683+00
2824	2026-10-29 16:00:00+00	sparql_query	\N	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	13	2026-10-29 16:20:19.983383+00
2825	2026-10-29 16:00:00+00	view	http://localhost:8000	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	11	2026-10-29 16:20:20.0132+00
2826	2026-10-29 16:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	16	2026-10-29 16:20:20.046116+00
2827	2026-10-29 16:00:00+00	view	http://localhost:8000/catalog/soil-and-groundwater-catalog	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	9	2026-10-29 16:20:20.071382+00
2828	2026-10-29 16:00:00+00	view	http://localhost:8000/catalog/environmental-monitoring	\N	\N	\N	235b7b66cecfdbb4fcd0d8d93247eb6f	200	65	2026-10-29 16:20:20.259585+00
\.


--
-- Data for Name: policy_decisions_audit; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.policy_decisions_audit (id) FROM stdin;
\.


--
-- Data for Name: profile_applied; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.profile_applied (id, name, version, applied_at, manifest_checksum) FROM stdin;
1	default	0.1.0	2026-08-26 07:41:21.938527+00	39fbf04bc2b48b640d1b6119f2df3881d1c3cd189cbb6b12336816fc00e7af8b
\.


--
-- Data for Name: record_audit; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.record_audit (id, record_iri, operation, subject, etag, occurred_at) FROM stdin;
1	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	378dc3a571063a9e6c37beadb58117a1	2026-08-26 09:21:44.981684+00
2	http://localhost:8000/catalog/dataset-catalog	create	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	870ee2c6b5a7f833f6b5545ac6fc2125	2026-08-26 10:09:04.584944+00
3	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	92362b3613a4aa78975f9c227b6d7ae0	2026-08-26 10:09:04.584944+00
4	http://localhost:8000/fdp-api/licenses/cc-by-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	edd827b44314468f14887de39822119c	2026-10-28 11:13:56.477389+00
5	http://localhost:8000/fdp-api/licenses/cc0-1.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	0c54708e03cc83b01d147f72c0c2f0a3	2026-10-28 11:13:56.535683+00
6	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b55c97c255c6c3994d0e980c18a971f3	2026-10-28 11:13:56.567923+00
7	http://localhost:8000/fdp-api/licenses/cc-by-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	edd827b44314468f14887de39822119c	2026-10-28 11:14:02.061962+00
8	http://localhost:8000/fdp-api/licenses/cc0-1.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	0c54708e03cc83b01d147f72c0c2f0a3	2026-10-28 11:14:02.09478+00
9	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b55c97c255c6c3994d0e980c18a971f3	2026-10-28 11:14:02.126316+00
10	http://localhost:8000/fdp-api/licenses/cc-by-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	edd827b44314468f14887de39822119c	2026-10-28 11:14:08.861488+00
11	http://localhost:8000/fdp-api/licenses/cc0-1.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	0c54708e03cc83b01d147f72c0c2f0a3	2026-10-28 11:14:08.890976+00
12	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b55c97c255c6c3994d0e980c18a971f3	2026-10-28 11:14:08.921444+00
13	http://localhost:8000/fdp-api/licenses/cc-by-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	edd827b44314468f14887de39822119c	2026-10-28 11:14:18.830376+00
14	http://localhost:8000/fdp-api/licenses/cc0-1.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	0c54708e03cc83b01d147f72c0c2f0a3	2026-10-28 11:14:18.864572+00
15	http://localhost:8000/fdp-api/licenses/cc-by-sa-4.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b55c97c255c6c3994d0e980c18a971f3	2026-10-28 11:14:18.90375+00
16	http://localhost:8000/catalog/environmental-monitoring	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	9fc6a0aeefc934b59b8116ff618d0f1d	2026-10-28 11:14:18.918307+00
17	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	7210af81a59bfe06774b003048a73724	2026-10-28 11:14:18.918307+00
18	http://localhost:8000/catalog/clinical-research	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	7741be5b0ae8d8e72815e1f23c484462	2026-10-28 11:14:19.019821+00
19	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	e8aaa3c8511b9cc9b8b2157dfc8943ab	2026-10-28 11:14:19.019821+00
20	http://localhost:8000/dataset/air-quality-enschede-2024	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	307b7d8d1c46d1027fd40d3f188de24a	2026-10-28 11:14:19.157882+00
21	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	db7c76bee5a3b0c11c2cb24cd16e9fa0	2026-10-28 11:14:19.157882+00
22	http://localhost:8000/dataset/river-water-temperature	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	569223e2fc3147b8bdd983ef05bfed6e	2026-10-28 11:14:19.25443+00
23	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	d8aae8a7b70cf38f3bbb5ec22e3ba0ec	2026-10-28 11:14:19.25443+00
24	http://localhost:8000/dataset/urban-tree-inventory	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	a1725fd6e758ce215c6930764f601b00	2026-10-28 11:14:19.344473+00
25	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	098c77776069bf72926da411775225f1	2026-10-28 11:14:19.344473+00
26	http://localhost:8000/dataset/biobank-sample-catalog	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	10394b9b03d128a6c8bdbd962db319fa	2026-10-28 11:14:19.404261+00
27	http://localhost:8000/catalog/clinical-research	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	6d15fa3d1211f1739a1f8be6ed4b26e8	2026-10-28 11:14:19.404261+00
28	http://localhost:8000/dataset/rare-disease-registry	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	0dc441334804abf22eaa73694ea74685	2026-10-28 11:14:19.465582+00
29	http://localhost:8000/catalog/clinical-research	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	7df327e90f9af7b0febbdca7dcaa9c77	2026-10-28 11:14:19.465582+00
30	http://localhost:8000/distribution/air-quality-2024-csv	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	8370b987fb19a61d1939d721734b2e7a	2026-10-28 11:14:19.525349+00
31	http://localhost:8000/dataset/air-quality-enschede-2024	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	3b2a9af2c7f6c0030198d286dd7d4ad6	2026-10-28 11:14:19.525349+00
32	http://localhost:8000/distribution/air-quality-2024-stations	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	0f3c34eb90fff930e7f66be8574e36ae	2026-10-28 11:14:19.640739+00
33	http://localhost:8000/dataset/air-quality-enschede-2024	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	2a90cc79c630c717e208809607cbb1da	2026-10-28 11:14:19.640739+00
34	http://localhost:8000/distribution/river-temperature-csv	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	326cc5b8d6f7ca33f813270d8d3395c1	2026-10-28 11:14:19.711983+00
35	http://localhost:8000/dataset/river-water-temperature	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	f166de13fba209eb7485dc2a86d83eab	2026-10-28 11:14:19.711983+00
36	http://localhost:8000/distribution/river-temperature-api	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	f5a56b39a8b394998fe3358423471f60	2026-10-28 11:14:19.769764+00
37	http://localhost:8000/dataset/river-water-temperature	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	c2b742d47ac035aeea24786a0c621cf7	2026-10-28 11:14:19.769764+00
38	http://localhost:8000/distribution/urban-trees-geojson	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	05cd73319d67b6765ca002a41b85892d	2026-10-28 11:14:19.832794+00
39	http://localhost:8000/dataset/urban-tree-inventory	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	c8b762639130059d22355ceead429788	2026-10-28 11:14:19.832794+00
40	http://localhost:8000/distribution/biobank-access-request	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	9476cff0663a80e734e5cf3bc1cc1ab2	2026-10-28 11:14:19.898375+00
41	http://localhost:8000/dataset/biobank-sample-catalog	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	ef443efc955f4d0bdbb8eafc79b09ace	2026-10-28 11:14:19.898375+00
42	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	c8787f62d720024a8888bcb2f4f8f54f	2026-10-28 11:58:28.850435+00
43	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	276805747ef36b7f4e60574aef911c25	2026-10-28 11:58:28.850435+00
44	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 11:58:28.984463+00
45	http://localhost:8000/dataset/noise-levels-enschede-airport-2025	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	1a43bd0c55154946999009c6cfc038f2	2026-10-28 11:58:38.307182+00
46	http://localhost:8000/catalog/soil-and-groundwater-catalog	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	02b021d6890b3b0033ba1d997ac5e852	2026-10-28 13:08:36.066806+00
47	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	0e28796c0896b2271a049d0e139350d1	2026-10-28 13:08:36.066806+00
48	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	efc2b1f6b2e9b98170a022d19cb3b7a8	2026-10-28 13:09:23.491437+00
49	http://localhost:8000/catalog/soil-and-groundwater-catalog	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	91496c4bd2c280aa72acfca47968c9dd	2026-10-28 13:09:23.491437+00
50	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:09:46.835545+00
51	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	PUBLISHED->DRAFT	2026-10-28 13:09:55.459983+00
52	http://localhost:8000/catalog/soil-and-groundwater-catalog	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	7507211c76017f24a7c38fc780efd095	2026-10-28 13:10:44.817597+00
53	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:11:17.500012+00
54	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	PUBLISHED->DRAFT	2026-10-28 13:11:49.911298+00
55	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:12:07.088205+00
56	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:12:15.299363+00
57	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	ecdd9daf4408e030a3c947b20b08a69b	2026-10-28 13:12:50.760279+00
58	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	feabf629d31ee8073758e27633e175c2	2026-10-28 13:14:41.583023+00
59	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	delete	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	\N	2026-10-28 13:16:55.225453+00
60	http://localhost:8000/catalog/soil-and-groundwater-catalog	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	af2f2e3762d2eaf20b1fbd4f26e8ad3c	2026-10-28 13:16:55.225453+00
61	http://localhost:8000/catalog/soil-and-groundwater-catalog	delete	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	\N	2026-10-28 13:16:55.367757+00
62	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	e8aaa3c8511b9cc9b8b2157dfc8943ab	2026-10-28 13:16:55.367757+00
63	http://localhost:8000/catalog/soil-and-groundwater-catalog	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	02b021d6890b3b0033ba1d997ac5e852	2026-10-28 13:19:31.797894+00
64	http://localhost:8000	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	0e28796c0896b2271a049d0e139350d1	2026-10-28 13:19:31.797894+00
65	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	create	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	efc2b1f6b2e9b98170a022d19cb3b7a8	2026-10-28 13:20:19.546229+00
66	http://localhost:8000/catalog/soil-and-groundwater-catalog	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	91496c4bd2c280aa72acfca47968c9dd	2026-10-28 13:20:19.546229+00
67	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:20:40.322026+00
68	http://localhost:8000/catalog/soil-and-groundwater-catalog	state_change	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	DRAFT->PUBLISHED	2026-10-28 13:20:48.495076+00
69	http://localhost:8000/dataset/groundwater-levels-twente-2020-to-2024	modify	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	ecdd9daf4408e030a3c947b20b08a69b	2026-10-28 13:21:24.330303+00
70	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b6d4b2954700a7293a662f7b5b99d52b	2026-10-28 13:49:13.36561+00
71	http://localhost:8000/fdp-api/licenses/odc-by-1.0	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	3ed886a6c911779329ac6bef04cb1208	2026-10-28 13:49:59.627181+00
72	http://localhost:8000/fdp-api/policies/curated-catalog	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	ccf89a1ef12a4c4cd53201408558d308	2026-10-28 13:53:14.480062+00
73	http://localhost:8000/catalog/environmental-monitoring	modify	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	b6d4b2954700a7293a662f7b5b99d52b	2026-10-28 13:53:42.779679+00
74	http://localhost:8000/fdp-api/policies/curated-catalog	state_change	http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	DRAFT->PUBLISHED	2026-10-28 13:55:51.388961+00
\.


--
-- Data for Name: runtime_settings; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.runtime_settings (key, value_json, updated_by, updated_at) FROM stdin;
\.


--
-- Data for Name: search_saved_queries; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.search_saved_queries (id, owner_subject, name, query_json, shared, created_at) FROM stdin;
d871ebfa-50af-436c-82d2-bfcaf0bcdb5c	http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	River temperature datasets	{"to": null, "from": null, "limit": null, "query": null, "types": [], "offset": 0, "license": null, "language": null}	f	2026-10-28 13:17:23.450654+00
\.


--
-- Data for Name: subject_principal; Type: TABLE DATA; Schema: public; Owner: fdp
--

COPY public.subject_principal (subject, roles_json, groups_json, updated_at) FROM stdin;
http://localhost:8081/realms/fdp-dev#50289f84-1eaa-4e42-8570-4e0a321e5144	["steward"]	[]	2026-10-28 13:17:58.196872+00
http://localhost:8081/realms/fdp-dev#e61e7fa7-3ac0-4e7f-9823-9d4e09007b58	["admin", "steward"]	[]	2026-10-29 13:41:50.085381+00
http://localhost:8081/realms/fdp-dev#ba454e0a-0e8c-438d-9443-f0e5c3ec497c	[]	[]	2026-10-29 16:20:20.23131+00
\.


--
-- Name: authz_index_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.authz_index_id_seq', 632, true);


--
-- Name: job_state_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.job_state_id_seq', 1, false);


--
-- Name: metrics_daily_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.metrics_daily_id_seq', 651, true);


--
-- Name: metrics_hourly_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.metrics_hourly_id_seq', 1439, true);


--
-- Name: metrics_raw_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.metrics_raw_id_seq', 2828, true);


--
-- Name: policy_decisions_audit_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.policy_decisions_audit_id_seq', 1, false);


--
-- Name: profile_applied_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.profile_applied_id_seq', 1, true);


--
-- Name: record_audit_id_seq; Type: SEQUENCE SET; Schema: public; Owner: fdp
--

SELECT pg_catalog.setval('public.record_audit_id_seq', 74, true);


--
-- Name: alembic_version alembic_version_pkc; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.alembic_version
    ADD CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num);


--
-- Name: api_keys api_keys_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.api_keys
    ADD CONSTRAINT api_keys_pkey PRIMARY KEY (id);


--
-- Name: authz_index authz_index_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.authz_index
    ADD CONSTRAINT authz_index_pkey PRIMARY KEY (id);


--
-- Name: index_targets index_targets_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.index_targets
    ADD CONSTRAINT index_targets_pkey PRIMARY KEY (id);


--
-- Name: index_targets index_targets_url_key; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.index_targets
    ADD CONSTRAINT index_targets_url_key UNIQUE (url);


--
-- Name: job_state job_state_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.job_state
    ADD CONSTRAINT job_state_pkey PRIMARY KEY (id);


--
-- Name: metadata_external_labels metadata_external_labels_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metadata_external_labels
    ADD CONSTRAINT metadata_external_labels_pkey PRIMARY KEY (iri, language);


--
-- Name: metadata_search metadata_search_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metadata_search
    ADD CONSTRAINT metadata_search_pkey PRIMARY KEY (record_iri);


--
-- Name: metrics_daily metrics_daily_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_daily
    ADD CONSTRAINT metrics_daily_pkey PRIMARY KEY (id);


--
-- Name: metrics_hourly metrics_hourly_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_hourly
    ADD CONSTRAINT metrics_hourly_pkey PRIMARY KEY (id);


--
-- Name: metrics_raw metrics_raw_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_raw
    ADD CONSTRAINT metrics_raw_pkey PRIMARY KEY (id);


--
-- Name: policy_decisions_audit policy_decisions_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.policy_decisions_audit
    ADD CONSTRAINT policy_decisions_audit_pkey PRIMARY KEY (id);


--
-- Name: profile_applied profile_applied_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.profile_applied
    ADD CONSTRAINT profile_applied_pkey PRIMARY KEY (id);


--
-- Name: record_audit record_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.record_audit
    ADD CONSTRAINT record_audit_pkey PRIMARY KEY (id);


--
-- Name: runtime_settings runtime_settings_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.runtime_settings
    ADD CONSTRAINT runtime_settings_pkey PRIMARY KEY (key);


--
-- Name: search_saved_queries search_saved_queries_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.search_saved_queries
    ADD CONSTRAINT search_saved_queries_pkey PRIMARY KEY (id);


--
-- Name: subject_principal subject_principal_pkey; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.subject_principal
    ADD CONSTRAINT subject_principal_pkey PRIMARY KEY (subject);


--
-- Name: authz_index uq_authz_subject_action_graph; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.authz_index
    ADD CONSTRAINT uq_authz_subject_action_graph UNIQUE (subject_key, action, graph_uri);


--
-- Name: metrics_daily uq_metrics_daily_dimensions; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_daily
    ADD CONSTRAINT uq_metrics_daily_dimensions UNIQUE NULLS NOT DISTINCT (bucket, event_type, resource_iri, country_code, region, city);


--
-- Name: metrics_hourly uq_metrics_hourly_dimensions; Type: CONSTRAINT; Schema: public; Owner: fdp
--

ALTER TABLE ONLY public.metrics_hourly
    ADD CONSTRAINT uq_metrics_hourly_dimensions UNIQUE NULLS NOT DISTINCT (bucket, event_type, resource_iri, country_code, region, city);


--
-- Name: ix_api_keys_owner_subject; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_api_keys_owner_subject ON public.api_keys USING btree (owner_subject);


--
-- Name: ix_authz_graph_uri; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_authz_graph_uri ON public.authz_index USING btree (graph_uri);


--
-- Name: ix_authz_subject_key; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_authz_subject_key ON public.authz_index USING btree (subject_key);


--
-- Name: ix_metadata_external_labels_expires_at; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_external_labels_expires_at ON public.metadata_external_labels USING btree (expires_at);


--
-- Name: ix_metadata_search_license; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_search_license ON public.metadata_search USING btree (license);


--
-- Name: ix_metadata_search_search_text; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_search_search_text ON public.metadata_search USING gin (search_text);


--
-- Name: ix_metadata_search_type_iri; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_search_type_iri ON public.metadata_search USING btree (type_iri);


--
-- Name: ix_metadata_search_updated_at; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_search_updated_at ON public.metadata_search USING btree (updated_at);


--
-- Name: ix_metadata_search_visibility; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metadata_search_visibility ON public.metadata_search USING btree (state, anon_read);


--
-- Name: ix_metrics_daily_bucket; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metrics_daily_bucket ON public.metrics_daily USING btree (bucket);


--
-- Name: ix_metrics_hourly_bucket; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metrics_hourly_bucket ON public.metrics_hourly USING btree (bucket);


--
-- Name: ix_metrics_raw_bucket; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metrics_raw_bucket ON public.metrics_raw USING btree (bucket);


--
-- Name: ix_metrics_raw_recorded_at; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_metrics_raw_recorded_at ON public.metrics_raw USING btree (recorded_at);


--
-- Name: ix_record_audit_occurred_at; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_record_audit_occurred_at ON public.record_audit USING btree (occurred_at);


--
-- Name: ix_record_audit_record_iri; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_record_audit_record_iri ON public.record_audit USING btree (record_iri);


--
-- Name: ix_record_audit_subject; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_record_audit_subject ON public.record_audit USING btree (subject);


--
-- Name: ix_search_saved_queries_owner; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_search_saved_queries_owner ON public.search_saved_queries USING btree (owner_subject);


--
-- Name: ix_search_saved_queries_shared; Type: INDEX; Schema: public; Owner: fdp
--

CREATE INDEX ix_search_saved_queries_shared ON public.search_saved_queries USING btree (shared);


--
-- Name: ux_api_keys_key_hash; Type: INDEX; Schema: public; Owner: fdp
--

CREATE UNIQUE INDEX ux_api_keys_key_hash ON public.api_keys USING btree (key_hash);


--
-- Name: ux_profile_applied_singleton; Type: INDEX; Schema: public; Owner: fdp
--

CREATE UNIQUE INDEX ux_profile_applied_singleton ON public.profile_applied USING btree ((true));


--
-- PostgreSQL database dump complete
--

\unrestrict 34MVcw68puyZbC1wVsTmIjOveC5rgAoTvRj7eCtWw42fPD5I6mcoWONmS84Weyo

--
-- Database "postgres" dump
--

\connect postgres

--
-- PostgreSQL database dump
--

\restrict LS3CkfhPa2ja3lufVCae55Ggl7TwEf3D104FcFQbZt8oIhi582xKag8qHVoVnHl

-- Dumped from database version 16.15 (Debian 16.15-1.pgdg13+2)
-- Dumped by pg_dump version 16.15 (Debian 16.15-1.pgdg13+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- PostgreSQL database dump complete
--

\unrestrict LS3CkfhPa2ja3lufVCae55Ggl7TwEf3D104FcFQbZt8oIhi582xKag8qHVoVnHl

--
-- PostgreSQL database cluster dump complete
--

