# PROVENANCE NOTES — extracted from live Clarivate OpenAPI definitions on 2026-10-19

## Web of Science Expanded API
servers: [{"url": "https://api.clarivate.com/api/wos"}]
securitySchemes: {"key": {"type": "apiKey", "in": "header", "name": "X-ApiKey"}}
- GET / :: databaseId(query,req=True,default=None); lang(query,req=False,default=None); usrQuery(query,req=True,default=None); edition(query,req=False,default=None); publishTimeSpan(query,req=False,default=None); loadTimeSpan(query,req=False,default=None); createdTimeSpan(query,req=False,default=None); modifiedTimeSpan(query,req=False,default=None); tcModifiedTimeSpan(query,req=False,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None); viewField(query,req=False,default=None); optionView(query,req=False,default=FR); optionOther(query,req=False,default=None); links(query,req=False,default=None)
- POST / :: 
    requestBody: yes
- GET /id/{uniqueId} :: databaseId(query,req=True,default=None); lang(query,req=False,default=None); uniqueId(path,req=True,default=None); viewField(query,req=False,default=None); optionView(query,req=False,default=FR); optionOther(query,req=False,default=None); links(query,req=False,default=None)
- GET /query/{queryId} :: queryId(path,req=True,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None); viewField(query,req=False,default=None); optionView(query,req=False,default=FR); optionOther(query,req=False,default=None); links(query,req=False,default=None)
- GET /recordids/{queryId} :: queryId(path,req=True,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None)
- GET /related :: databaseId(query,req=True,default=None); lang(query,req=False,default=None); uniqueId(query,req=True,default=None); edition(query,req=False,default=None); publishTimeSpan(query,req=False,default=None); modifiedTimeSpan(query,req=False,default=None); tcModifiedTimeSpan(query,req=False,default=None); refId(query,req=False,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None); viewField(query,req=False,default=None); optionView(query,req=False,default=FR); optionOther(query,req=False,default=None); links(query,req=False,default=None)
- GET /citing :: databaseId(query,req=True,default=None); lang(query,req=False,default=None); uniqueId(query,req=True,default=None); edition(query,req=False,default=None); publishTimeSpan(query,req=False,default=None); modifiedTimeSpan(query,req=False,default=None); tcModifiedTimeSpan(query,req=False,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None); viewField(query,req=False,default=None); optionView(query,req=False,default=FR); optionOther(query,req=False,default=None); links(query,req=False,default=None)
- GET /references :: databaseId(query,req=True,default=None); lang(query,req=False,default=None); uniqueId(query,req=True,default=None); count(query,req=False,default=10); firstRecord(query,req=False,default=1); sortField(query,req=False,default=None); optionOther(query,req=False,default=None)
- GET /category-context/{uniqueId} :: uniqueId(path,req=True,default=None); min(query,req=False,default=None); maxCat(query,req=False,default=None); scope(query,req=False,default=None)
- GET /citation-report/{queryId} :: queryId(path,req=True,default=None); reportLevel(query,req=False,default=WOS); startYear(query,req=False,default=1900); endYear(query,req=False,default=Current Year)

## InCites Document Level Metrics API v2.0
servers: [{"url": "https://incites-api.clarivate.com/api/incites"}]
securitySchemes: {"key": {"type": "apiKey", "in": "header", "name": "X-ApiKey"}}
- POST /DocumentLevelMetricsByUTforWOS/{format} :: format(path,req=True,default=None); X-ApiKey(header,req=True,default=None)
    requestBody: yes
- GET /InCitesLastUpdated/{format} :: format(path,req=True,default=None); X-ApiKey(header,req=True,default=None)
- GET /DocumentLevelMetricsByUT/{format} :: format(path,req=True,default=None); UT(query,req=True,default=None); esci(query,req=False,default=n); ver(query,req=True,default=None); schema(query,req=False,default=wos); X-ApiKey(header,req=True,default=None)
- GET /DocumentLevelMetricsByInstitutionIdRecordCount/{format} :: format(path,req=True,default=None); year(query,req=True,default=None); X-ApiKey(header,req=True,default=None)
- GET /DocumentLevelMetricsByInstitutionId/{format} :: format(path,req=True,default=None); year(query,req=True,default=None); startingrecord(query,req=True,default=None); recordcount(query,req=True,default=None); esci(query,req=False,default=n); ver(query,req=True,default=None); schema(query,req=False,default=wos); X-ApiKey(header,req=True,default=None)
- GET /CheckValidUTs/{format} :: format(path,req=True,default=None); UT(query,req=True,default=None); X-ApiKey(header,req=True,default=None)
