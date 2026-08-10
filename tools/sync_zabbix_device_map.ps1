param(
    [string]$ZabbixUrl = "",
    [string]$User = "",
    [string]$Password = "",
    [string]$MapName = "",
    [string]$CsvPath = ".\data\devices.csv",
    [ValidateSet("camera", "recorder", "switch", "cabinet")]
    [string]$DeviceType = "camera",
    [string]$IconName = "",
    [string]$BackupDir = ".\backups\maps",
    [switch]$Apply
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$requiredParameters = @{ ZabbixUrl = $ZabbixUrl; User = $User; Password = $Password; MapName = $MapName }
$missingParameters = @($requiredParameters.GetEnumerator() | Where-Object { [string]::IsNullOrWhiteSpace([string]$_.Value) } | ForEach-Object Key)
if ($missingParameters.Count -gt 0) {
    throw "Zabbix connection is not configured. Missing parameters: $($missingParameters -join ', ')."
}
$Invariant = [System.Globalization.CultureInfo]::InvariantCulture
$Timestamp = Get-Date -Format "yyyy-MM-dd_HH-mm-ss"

if (!(Test-Path $BackupDir)) {
    New-Item -ItemType Directory -Path $BackupDir -Force | Out-Null
}

$LogFile = Join-Path $BackupDir "sync_${DeviceType}_$Timestamp.log"
$BackupBefore = Join-Path $BackupDir "map_before_${DeviceType}_$Timestamp.json"
$PlanFile = Join-Path $BackupDir "map_plan_${DeviceType}_$Timestamp.json"
$BackupAfter = Join-Path $BackupDir "map_after_${DeviceType}_$Timestamp.json"

function Write-Log {
    param([string]$Text)
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') - $Text"
    Write-Host $line
    Add-Content -Path $LogFile -Value $line -Encoding UTF8
}

$script:ApiId = 1

function Get-PropertyValue {
    param([object]$Object, [string]$Name, [object]$Default = $null)
    if ($null -eq $Object) { return $Default }
    $property = $Object.PSObject.Properties[$Name]
    if ($null -eq $property) { return $Default }
    return $property.Value
}

function Invoke-ZabbixApi {
    param(
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)][object]$Params,
        [string]$Token = $null
    )

    $payload = @{
        jsonrpc = "2.0"
        method = $Method
        params = $Params
        id = $script:ApiId
    }
    $script:ApiId++

    $headers = @{}
    if ($Token) { $headers["Authorization"] = "Bearer $Token" }

    $json = $payload | ConvertTo-Json -Depth 100 -Compress
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [byte[]]$bodyBytes = $utf8NoBom.GetBytes($json)

    if ($Method -eq "map.update") {
        $debugFile = Join-Path $BackupDir "map_update_${DeviceType}_$Timestamp.json"
        [System.IO.File]::WriteAllText($debugFile, $json, $utf8NoBom)
        Write-Log "Request map.update: $debugFile"
    }

    $response = Invoke-RestMethod `
        -Uri $ZabbixUrl `
        -Method Post `
        -ContentType "application/json-rpc; charset=utf-8" `
        -Headers $headers `
        -Body $bodyBytes

    if ($null -eq $response) {
        throw "Zabbix API [$Method] zwróciło pustą odpowiedź."
    }

    $errorProperty = $response.PSObject.Properties["error"]
    if ($null -ne $errorProperty) {
        $apiError = $errorProperty.Value
        $message = [string](Get-PropertyValue -Object $apiError -Name "message" -Default "Nieznany błąd")
        $data = [string](Get-PropertyValue -Object $apiError -Name "data" -Default "")
        throw "Zabbix API error [$Method]: $message / $data"
    }

    $resultProperty = $response.PSObject.Properties["result"]
    if ($null -eq $resultProperty) {
        throw "Zabbix API [$Method] nie zwróciło pola result."
    }
    return $resultProperty.Value
}

function Add-OptionalProperty {
    param([hashtable]$Target, [object]$Source, [string]$Name)
    $value = Get-PropertyValue -Object $Source -Name $Name
    if ($null -ne $value -and [string]$value -ne "") {
        $Target[$Name] = $value
    }
}

function Convert-UrlsToWritable {
    param([object]$Urls)
    $result = @()
    foreach ($url in @($Urls)) {
        $name = [string](Get-PropertyValue -Object $url -Name "name" -Default "")
        $value = [string](Get-PropertyValue -Object $url -Name "url" -Default "")
        if (![string]::IsNullOrWhiteSpace($name) -and ![string]::IsNullOrWhiteSpace($value)) {
            $result += @{ name = $name; url = $value }
        }
    }
    return @($result)
}

function Get-SelementUrls {
    param([object]$Selement)
    # Elementy graficzne, np. szafy, celowo nie mają pola `urls`.
    # Zabbix nie zwraca wtedy pustej listy, tylko całkowicie pomija to pole.
    # W trybie StrictMode nie wolno odwoływać się bezpośrednio do
    # `$Selement.urls`, bo sam odczyt takiego brakującego pola kończył
    # kontrolę mapy błędem. Zawsze zwracamy bezpieczną pustą kolekcję.
    $urls = Get-PropertyValue -Object $Selement -Name "urls" -Default $null
    if ($null -eq $urls) { return @() }
    return @($urls)
}

function Get-SelementIpFromUrls {
    param([object]$Selement)
    # Host usunięty z Zabbixa może pozostawić na mapie stary selement. Jego
    # techniczny hostid nie jest już wtedy możliwy do przetłumaczenia przez
    # host.get, ale adres IP pozostaje w utworzonym wcześniej linku "Otworz…".
    # Dzięki temu możemy bezpiecznie rozpoznać wyłącznie osierocony punkt,
    # dla którego lokalny wpis tego samego typu ma status SKIPPED/PENDING.
    foreach ($url in @(Get-SelementUrls -Selement $Selement)) {
        $value = [string](Get-PropertyValue -Object $url -Name "url" -Default "")
        if ($value -match '(?i)^https?://(\d{1,3}(?:\.\d{1,3}){3})(?:[/:?#]|$)') {
            return [string]$Matches[1]
        }
    }
    return ""
}

function Convert-SelementToWritable {
    param([object]$Selement)
    $result = @{
        selementid = [string](Get-PropertyValue -Object $Selement -Name "selementid")
        elementtype = [int](Get-PropertyValue -Object $Selement -Name "elementtype" -Default 4)
        iconid_off = [string](Get-PropertyValue -Object $Selement -Name "iconid_off")
        x = [int](Get-PropertyValue -Object $Selement -Name "x" -Default 0)
        y = [int](Get-PropertyValue -Object $Selement -Name "y" -Default 0)
    }

    $elements = @()
    foreach ($element in @(Get-PropertyValue -Object $Selement -Name "elements" -Default @())) {
        $copy = @{}
        foreach ($key in @("hostid", "groupid", "sysmapid", "triggerid")) {
            $value = Get-PropertyValue -Object $element -Name $key
            if ($null -ne $value -and [string]$value -ne "") {
                $copy[$key] = [string]$value
            }
        }
        if ($copy.Count -gt 0) { $elements += $copy }
    }
    if ($elements.Count -gt 0) { $result["elements"] = @($elements) }

    foreach ($name in @(
        "iconid_on", "iconid_disabled", "iconid_maintenance", "label",
        "label_location", "show_label", "use_iconmap", "evaltype",
        "elementsubtype", "areatype", "width", "height", "viewtype", "zindex"
    )) {
        Add-OptionalProperty -Target $result -Source $Selement -Name $name
    }

    # Nawet gdy element (np. szafa) nie ma żadnych URL-i, polecenie
    # Convert-UrlsToWritable nie emituje wtedy nic do potoku. Otaczamy wynik
    # @(), aby $urls zawsze było kolekcją z właściwością Count; w StrictMode
    # samo $null.Count kończyło synchronizację błędem.
    $urls = @(Convert-UrlsToWritable -Urls (Get-SelementUrls -Selement $Selement))
    if ($urls.Count -gt 0) { $result["urls"] = @($urls) }
    return $result
}

function Set-DeviceUrl {
    param([hashtable]$Selement, [string]$Ip, [string]$Type)
    $urls = @()
    foreach ($url in @(Get-SelementUrls -Selement $Selement)) {
        if ([string]$url.name -notlike "Otworz *") {
            $urls += @{ name = [string]$url.name; url = [string]$url.url }
        }
    }
    $label = switch ($Type) {
        "camera" { "kamere" }
        "recorder" { "rejestrator" }
        "switch" { "switch" }
        default { "urzadzenie" }
    }
    $urls += @{ name = "Otworz $label $Ip"; url = "http://$Ip" }
    $Selement["urls"] = @($urls)
}

function Set-DeviceIcons {
    param([hashtable]$Selement, [hashtable]$Icons)
    $Selement["iconid_off"] = [string]$Icons["off"]
    foreach ($pair in @(
        @("on", "iconid_on"),
        @("disabled", "iconid_disabled"),
        @("maintenance", "iconid_maintenance")
    )) {
        if ($Icons.ContainsKey($pair[0])) {
            $Selement[$pair[1]] = [string]$Icons[$pair[0]]
        }
    }
}

function New-DeviceSelement {
    param(
        [object]$Template,
        [hashtable]$Icons,
        [string]$TemporaryId,
        [string]$HostId,
        [int]$X,
        [int]$Y,
        [string]$Ip,
        [string]$Type
    )

    $result = @{
        selementid = $TemporaryId
        elements = @(@{ hostid = $HostId })
        elementtype = 0
        iconid_off = [string]$Icons["off"]
        x = $X
        y = $Y
        label = ""
    }
    if ($null -ne $Template) {
        foreach ($name in @("label_location", "show_label", "use_iconmap", "zindex")) {
            Add-OptionalProperty -Target $result -Source $Template -Name $name
        }
    }
    Set-DeviceIcons -Selement $result -Icons $Icons
    Set-DeviceUrl -Selement $result -Ip $Ip -Type $Type
    return $result
}

function Get-LegacyCabinetIdFromSelement {
    param([object]$Selement)
    # V3.0.0 rozpoznawał szafę po technicznym linku CCTV_TOOL_ID. Ten kod
    # pozostaje tylko na czas migracji już istniejących ikon.
    if ([int](Get-PropertyValue -Object $Selement -Name "elementtype" -Default -1) -ne 4) {
        return ""
    }
    foreach ($url in @(Get-SelementUrls -Selement $Selement)) {
        $name = [string](Get-PropertyValue -Object $url -Name "name" -Default "")
        $value = [string](Get-PropertyValue -Object $url -Name "url" -Default "")
        if ($name -eq "CCTV_TOOL_ID" -and $value -match "/cabinet/([^/?#]+)$") {
            return [uri]::UnescapeDataString($Matches[1])
        }
    }
    return ""
}

function Get-CabinetMapElementId {
    param([object]$Row, [string]$MapName)
    $raw = [string](Get-PropertyValue -Object $Row -Name "MapElementIds" -Default "")
    if ([string]::IsNullOrWhiteSpace($raw)) { return "" }
    try {
        $registry = $raw | ConvertFrom-Json -ErrorAction Stop
        $property = $registry.PSObject.Properties[$MapName]
        if ($null -ne $property -and $null -ne $property.Value) {
            return ([string]$property.Value).Trim()
        }
    }
    catch {
        # Stary lub ręcznie uszkodzony wpis nie blokuje zwykłego importu.
    }
    return ""
}

function Get-CabinetLocalIdFromSelement {
    param([object]$Selement, [hashtable]$CachedIdsByElementId)
    if ([int](Get-PropertyValue -Object $Selement -Name "elementtype" -Default -1) -ne 4) {
        return ""
    }
    $elementId = ([string](Get-PropertyValue -Object $Selement -Name "selementid" -Default "")).Trim()
    if ($elementId -and $CachedIdsByElementId.ContainsKey($elementId)) {
        return [string]$CachedIdsByElementId[$elementId]
    }
    return Get-LegacyCabinetIdFromSelement -Selement $Selement
}

function Clear-CabinetUrls {
    param([hashtable]$Selement)
    # Szafa nie ma klikanych akcji. Usuwamy też dawny CCTV_TOOL_ID z V3.0.0.
    if ($Selement.ContainsKey("urls")) { $Selement.Remove("urls") | Out-Null }
}

function New-CabinetSelement {
    param(
        [string]$TemporaryId,
        [string]$IconId,
        [int]$X,
        [int]$Y,
        [string]$Label
    )
    $result = @{
        selementid = $TemporaryId
        elementtype = 4
        iconid_off = $IconId
        x = $X
        y = $Y
        label = $Label
        label_location = 0
        # Szafa ma zawsze pokazywać krótką nazwę pod ikoną. To nie jest
        # techniczna nazwa elementu typu "Image", lecz nazwa podana w narzędziu.
        show_label = 0
    }
    return $result
}

function Parse-DoubleInvariant {
    param([string]$Value)
    $number = 0.0
    $ok = [double]::TryParse(
        $Value,
        [System.Globalization.NumberStyles]::Float,
        $Invariant,
        [ref]$number
    )
    if (!$ok) { throw "Nie można odczytać liczby: '$Value'" }
    return $number
}

Write-Log "=== SYNCHRONIZACJA MAPY CCTV — TYP: $DeviceType ==="
Write-Log "Tryb: $(if ($Apply) { 'APPLY' } else { 'DRY RUN' })"
Write-Log "Mapa: $MapName"
Write-Log "CSV: $CsvPath"

if (!(Test-Path $CsvPath)) { throw "Nie znaleziono devices.csv: $CsvPath" }

$allRows = @(Import-Csv -Path $CsvPath -Delimiter ";")
$rows = @($allRows | Where-Object { ([string]$_.Type).Trim().ToLowerInvariant() -eq $DeviceType })
$placedRows = @($rows | Where-Object { ([string]$_.MapStatus).Trim().ToUpperInvariant() -eq "PLACED" })
$skippedRows = @($rows | Where-Object { ([string]$_.MapStatus).Trim().ToUpperInvariant() -eq "SKIPPED" })
$pendingRows = @($rows | Where-Object { ([string]$_.MapStatus).Trim().ToUpperInvariant() -eq "PENDING" })

Write-Log "Typ ${DeviceType}: TOTAL=$($rows.Count), PLACED=$($placedRows.Count), SKIPPED=$($skippedRows.Count), PENDING=$($pendingRows.Count)"
if ($rows.Count -eq 0) { throw "Brak urządzeń typu '$DeviceType' w devices.csv." }
if ($placedRows.Count -eq 0) { Write-Log "Brak urządzeń PLACED. Istniejące punkty tego typu zostaną usunięte z mapy." }
if ($pendingRows.Count -gt 0) { Write-Log "UWAGA: PENDING=$($pendingRows.Count). Te urządzenia nie będą widoczne na mapie." }

$duplicateHostNames = @($placedRows | Group-Object HostName | Where-Object { $_.Count -gt 1 })
if ($duplicateHostNames.Count -gt 0) {
    $names = $duplicateHostNames | ForEach-Object { $_.Name }
    throw "Duplikaty HostName: $($names -join ', ')"
}

try {
    $token = Invoke-ZabbixApi -Method "user.login" -Params @{ username = $User; password = $Password }
}
catch {
    $token = Invoke-ZabbixApi -Method "user.login" -Params @{ user = $User; password = $Password }
}
Write-Log "Zalogowano do Zabbixa."

$maps = @(Invoke-ZabbixApi -Method "map.get" -Token $token -Params @{
    output = "extend"
    filter = @{ name = @($MapName) }
    selectSelements = "extend"
    selectLinks = "extend"
    selectUrls = "extend"
    selectShapes = "extend"
    selectLines = "extend"
})
if ($maps.Count -ne 1) { throw "Nie znaleziono dokładnie jednej mapy '$MapName'." }

$map = $maps[0]
$sysmapid = [string]$map.sysmapid
$mapWidth = [int]$map.width
$mapHeight = [int]$map.height
$existingSelements = @($map.selements)
$map | ConvertTo-Json -Depth 100 | Set-Content -Path $BackupBefore -Encoding UTF8
Write-Log "Mapa: sysmapid=$sysmapid, ${mapWidth}x${mapHeight}"
Write-Log "Backup przed zmianą: $BackupBefore"

$isCabinet = $DeviceType -eq "cabinet"
$allTypeIds = @($rows | ForEach-Object { ([string]$_.HostName).Trim() } | Where-Object { $_ -ne "" } | Sort-Object -Unique)
$placedIds = @($placedRows | ForEach-Object { ([string]$_.HostName).Trim() } | Where-Object { $_ -ne "" } | Sort-Object -Unique)
if ($isCabinet -and $allTypeIds.Count -ne $rows.Count) {
    throw "Każda szafa musi mieć lokalny identyfikator. Dodaj ją ponownie przez narzędzie."
}
$allTypeIdSet = @{}
foreach ($identifier in $allTypeIds) { $allTypeIdSet[$identifier] = $true }
$placedIdSet = @{}
foreach ($identifier in $placedIds) { $placedIdSet[$identifier] = $true }
$unplacedIpSet = @{}
if (!$isCabinet) {
    foreach ($row in @($skippedRows + $pendingRows)) {
        $ip = ([string]$row.IP).Trim()
        if ($ip) { $unplacedIpSet[$ip] = $true }
    }
}

# Od V3.0.1 identyfikator elementu mapy jest przechowywany lokalnie w CSV,
# osobno dla każdej mapy. Dzięki temu ikona szafy nie ma sztucznego linku.
$cabinetLocalIdByElementId = @{}
if ($isCabinet) {
    foreach ($cabinetRow in $rows) {
        $localId = ([string]$cabinetRow.HostName).Trim()
        $elementId = Get-CabinetMapElementId -Row $cabinetRow -MapName $MapName
        if (!$localId -or !$elementId) { continue }
        if ($cabinetLocalIdByElementId.ContainsKey($elementId) -and $cabinetLocalIdByElementId[$elementId] -ne $localId) {
            throw "Ten sam element mapy jest przypisany do dwóch szaf: $elementId"
        }
        $cabinetLocalIdByElementId[$elementId] = $localId
    }
}

$hostNames = @()
$hostByName = @{}
$hostById = @{}
$mapHostById = @{}
$templateSelement = $null
if (!$isCabinet) {
    $hostNames = $placedIds
    $hosts = @()
    if ($hostNames.Count -gt 0) {
        $hosts = @(Invoke-ZabbixApi -Method "host.get" -Token $token -Params @{
            output = @("hostid", "host", "name", "status")
            filter = @{ host = $hostNames }
        })
    }
    foreach ($zbxHost in $hosts) {
        $hostByName[[string]$zbxHost.host] = $zbxHost
        $hostById[[string]$zbxHost.hostid] = $zbxHost
    }
    $missingHosts = @($hostNames | Where-Object { !$hostByName.ContainsKey($_) })
    if ($missingHosts.Count -gt 0) { throw "Brak hostów w Zabbixie: $($missingHosts -join ', ')" }
    Write-Log "Wszystkie hosty istnieją w Zabbixie: $($hosts.Count)."

    $mapHostIds = @()
    foreach ($selement in $existingSelements) {
        if ([int]$selement.elementtype -ne 0) { continue }
        foreach ($element in @($selement.elements)) {
            $hostId = Get-PropertyValue -Object $element -Name "hostid"
            if ($null -ne $hostId -and [string]$hostId -ne "") { $mapHostIds += [string]$hostId }
        }
    }
    if ($mapHostIds.Count -gt 0) {
        $mapHosts = @(Invoke-ZabbixApi -Method "host.get" -Token $token -Params @{
            output = @("hostid", "host", "name")
            hostids = @($mapHostIds | Sort-Object -Unique)
        })
        foreach ($zbxMapHost in $mapHosts) { $mapHostById[[string]$zbxMapHost.hostid] = $zbxMapHost }
    }

    foreach ($selement in $existingSelements) {
        if ([int]$selement.elementtype -ne 0) { continue }
        $elements = @($selement.elements)
        if ($elements.Count -eq 0) { continue }
        $hostId = [string](Get-PropertyValue -Object $elements[0] -Name "hostid")
        if ($hostById.ContainsKey($hostId)) { $templateSelement = $selement; break }
    }
    if ($null -eq $templateSelement -and $DeviceType -eq "camera") {
        foreach ($selement in $existingSelements) {
            if ([int]$selement.elementtype -ne 0) { continue }
            $elements = @($selement.elements)
            if ($elements.Count -eq 0) { continue }
            $hostId = [string](Get-PropertyValue -Object $elements[0] -Name "hostid")
            if ($mapHostById.ContainsKey($hostId) -and [string]$mapHostById[$hostId].host -like "CAM_*") {
                $templateSelement = $selement
                break
            }
        }
    }
}
else {
    Write-Log "Szafy są synchronizowane jako elementy graficzne bez hostów Zabbixa."
}

$icons = @{}
# Ikona jest potrzebna nie tylko do dodawania PLACED. Jest także bezpiecznym
# drugim warunkiem przy sprzątaniu osieroconego punktu po skasowanym hoście.
if ($rows.Count -gt 0) {
    if ($DeviceType -eq "camera" -and $null -ne $templateSelement) {
        $icons["off"] = [string](Get-PropertyValue -Object $templateSelement -Name "iconid_off")
        foreach ($pair in @(@("on", "iconid_on"), @("disabled", "iconid_disabled"), @("maintenance", "iconid_maintenance"))) {
            $value = Get-PropertyValue -Object $templateSelement -Name $pair[1]
            if ($null -ne $value -and [string]$value -ne "") { $icons[$pair[0]] = [string]$value }
        }
        Write-Log "Kamery: skopiowano pełny zestaw ikon stanów z istniejącej kamery."
    }
    else {
        if ([string]::IsNullOrWhiteSpace($IconName)) { throw "Brak nazwy ikony dla typu '$DeviceType'." }
        $imageNames = @($IconName, "${IconName}_PROBLEM", "${IconName}_DISABLED", "${IconName}_MAINTENANCE")
        $images = @(Invoke-ZabbixApi -Method "image.get" -Token $token -Params @{
            output = @("imageid", "name", "imagetype")
            filter = @{ name = $imageNames }
        })
        $imageByName = @{}
        foreach ($image in $images) { $imageByName[[string]$image.name] = [string]$image.imageid }
        if (!$imageByName.ContainsKey($IconName)) { throw "Nie znaleziono obrazu Zabbixa o nazwie '$IconName'." }
        $icons["off"] = $imageByName[$IconName]
        $icons["on"] = if ($imageByName.ContainsKey("${IconName}_PROBLEM")) { $imageByName["${IconName}_PROBLEM"] } else { $icons["off"] }
        $icons["disabled"] = if ($imageByName.ContainsKey("${IconName}_DISABLED")) { $imageByName["${IconName}_DISABLED"] } else { $icons["off"] }
        $icons["maintenance"] = if ($imageByName.ContainsKey("${IconName}_MAINTENANCE")) { $imageByName["${IconName}_MAINTENANCE"] } else { $icons["off"] }
    }
    Write-Log "Ikony gotowe. Domyślna imageid=$($icons['off'])"
}
$targetIconIds = @(
    $icons.Values |
        ForEach-Object { ([string]$_).Trim() } |
        Where-Object { $_ -ne "" } |
        Sort-Object -Unique
)

$existingTargetByKey = @{}
$duplicateElements = @()
foreach ($selement in $existingSelements) {
    $key = ""
    if ($isCabinet) {
        $key = Get-CabinetLocalIdFromSelement -Selement $selement -CachedIdsByElementId $cabinetLocalIdByElementId
    }
    elseif ([int]$selement.elementtype -eq 0) {
        $elements = @($selement.elements)
        if ($elements.Count -gt 0) { $key = [string](Get-PropertyValue -Object $elements[0] -Name "hostid") }
        if (!$hostById.ContainsKey($key)) { $key = "" }
    }
    if ($key -ne "") {
        if ($existingTargetByKey.ContainsKey($key)) { $duplicateElements += $key }
        else { $existingTargetByKey[$key] = $selement }
    }
}

# Dodatkowe zabezpieczenie: jeżeli CSV nie ma jeszcze lokalnego identyfikatora
# (np. po ręcznej naprawie pliku), rozpoznaj istniejącą szafę po jednoznacznej
# etykiecie. Nie tworzy to żadnego linku na mapie.
if ($isCabinet) {
    foreach ($row in $rows) {
        $localId = ([string]$row.HostName).Trim()
        if (!$localId -or $existingTargetByKey.ContainsKey($localId)) { continue }
        $label = ([string]$row.VisibleName).Trim()
        if (!$label) { continue }
        $matches = @(
            $existingSelements | Where-Object {
                [int](Get-PropertyValue -Object $_ -Name "elementtype" -Default -1) -eq 4 -and
                ([string](Get-PropertyValue -Object $_ -Name "label" -Default "")).Trim() -eq $label
            }
        )
        if ($matches.Count -eq 1) {
            $existingTargetByKey[$localId] = $matches[0]
        }
    }
}
if ($duplicateElements.Count -gt 0) { throw "Duplikaty elementów wybranego typu na mapie: $($duplicateElements -join ', ')" }

$finalSelements = @()
$removedTargetKeys = @{}
$removedCount = 0
foreach ($selement in $existingSelements) {
    $removeFromMap = $false
    if ($isCabinet) {
        $cabinetId = Get-CabinetLocalIdFromSelement -Selement $selement -CachedIdsByElementId $cabinetLocalIdByElementId
        if ($cabinetId -eq "") {
            foreach ($pair in $existingTargetByKey.GetEnumerator()) {
                if ([string]$pair.Value.selementid -eq [string]$selement.selementid) {
                    $cabinetId = [string]$pair.Key
                    break
                }
            }
        }
        if ($cabinetId -ne "" -and $allTypeIdSet.ContainsKey($cabinetId) -and !$placedIdSet.ContainsKey($cabinetId)) {
            $removeFromMap = $true
            $removedTargetKeys[$cabinetId] = $true
        }
    }
    elseif ([int]$selement.elementtype -eq 0) {
        $elements = @($selement.elements)
        if ($elements.Count -gt 0) {
            $hostId = [string](Get-PropertyValue -Object $elements[0] -Name "hostid")
            if ($mapHostById.ContainsKey($hostId)) {
                $hostName = [string]$mapHostById[$hostId].host
                if ($allTypeIdSet.ContainsKey($hostName) -and !$hostById.ContainsKey($hostId)) {
                    $removeFromMap = $true
                    $removedTargetKeys[$hostId] = $true
                }
            }
            elseif ($targetIconIds -contains ([string](Get-PropertyValue -Object $selement -Name "iconid_off" -Default ""))) {
                # Host nie istnieje już w Zabbixie. Usuwamy jego punkt wyłącznie
                # wtedy, gdy ikona pasuje do aktualnie synchronizowanego typu
                # oraz ten sam IP w devices.csv jest świadomie SKIPPED/PENDING.
                $orphanIp = Get-SelementIpFromUrls -Selement $selement
                if ($orphanIp -and $unplacedIpSet.ContainsKey($orphanIp)) {
                    $removeFromMap = $true
                    $removedTargetKeys["ORPHAN:$hostId"] = $true
                    Write-Log "Osierocony punkt usunięty z planu: hostid=$hostId, IP=$orphanIp"
                }
            }
        }
    }
    if ($removeFromMap) { $removedCount++ }
    else { $finalSelements += Convert-SelementToWritable -Selement $selement }
}

$maxExistingId = 0L
foreach ($selement in $existingSelements) {
    $id = 0L
    if ([long]::TryParse([string]$selement.selementid, [ref]$id) -and $id -gt $maxExistingId) { $maxExistingId = $id }
}

$tempCounter = 1L
$updatedCount = 0
$newCount = 0
$targetKeys = @{}
foreach ($row in $placedRows) {
    $localId = ([string]$row.HostName).Trim()
    $xPercent = Parse-DoubleInvariant -Value ([string]$row.XPercent)
    $yPercent = Parse-DoubleInvariant -Value ([string]$row.YPercent)
    $x = [int][math]::Round(($xPercent / 100.0) * $mapWidth)
    $y = [int][math]::Round(($yPercent / 100.0) * $mapHeight)
    $x = [math]::Max(0, [math]::Min($mapWidth - 1, $x))
    $y = [math]::Max(0, [math]::Min($mapHeight - 1, $y))

    if ($isCabinet) {
        $targetKeys[$localId] = $true
        if ($existingTargetByKey.ContainsKey($localId)) {
            $existingId = [string]$existingTargetByKey[$localId].selementid
            for ($index = 0; $index -lt $finalSelements.Count; $index++) {
                if ([string]$finalSelements[$index].selementid -eq $existingId) {
                    $finalSelements[$index].x = $x
                    $finalSelements[$index].y = $y
                    $finalSelements[$index].iconid_off = [string]$icons["off"]
                    $finalSelements[$index].label = ([string]$row.VisibleName).Trim()
                    $finalSelements[$index].label_location = 0
                    # Nazwa szafy jest stale widoczna pod ikoną, aby łatwo
                    # odróżnić kilka szaf na tej samej hali.
                    $finalSelements[$index].show_label = 0
                    Clear-CabinetUrls -Selement $finalSelements[$index]
                    break
                }
            }
            $updatedCount++
        }
        else {
            $temporaryId = [string]($maxExistingId + 1000000L + $tempCounter)
            $tempCounter++
            $finalSelements += New-CabinetSelement -TemporaryId $temporaryId -IconId $icons["off"] -X $x -Y $y -Label ([string]$row.VisibleName).Trim()
            $newCount++
        }
        continue
    }

    $ip = ([string]$row.IP).Trim()
    $hostId = [string]$hostByName[$localId].hostid
    $targetKeys[$hostId] = $true
    if ($existingTargetByKey.ContainsKey($hostId)) {
        $existingId = [string]$existingTargetByKey[$hostId].selementid
        for ($index = 0; $index -lt $finalSelements.Count; $index++) {
            if ([string]$finalSelements[$index].selementid -eq $existingId) {
                $finalSelements[$index].x = $x
                $finalSelements[$index].y = $y
                Set-DeviceIcons -Selement $finalSelements[$index] -Icons $icons
                Set-DeviceUrl -Selement $finalSelements[$index] -Ip $ip -Type $DeviceType
                break
            }
        }
        $updatedCount++
    }
    else {
        $temporaryId = [string]($maxExistingId + 1000000L + $tempCounter)
        $tempCounter++
        $finalSelements += New-DeviceSelement -Template $templateSelement -Icons $icons -TemporaryId $temporaryId -HostId $hostId -X $x -Y $y -Ip $ip -Type $DeviceType
        $newCount++
    }
}

$plan = @{
    generated_at = (Get-Date).ToString("s")
    mode = $(if ($Apply) { "APPLY" } else { "DRY_RUN" })
    type = $DeviceType
    map = @{ sysmapid = $sysmapid; name = [string]$map.name; width = $mapWidth; height = $mapHeight }
    csv = @{ total_type = $rows.Count; placed = $placedRows.Count; skipped = $skippedRows.Count; pending = $pendingRows.Count }
    changes = @{
        existing_selected_elements_updated = $updatedCount
        new_selected_elements_added = $newCount
        selected_elements_removed = $removedCount
        other_existing_map_elements_preserved = $existingSelements.Count - $removedCount
        final_element_count = $finalSelements.Count
    }
    selements = $finalSelements
}
$plan | ConvertTo-Json -Depth 100 | Set-Content -Path $PlanFile -Encoding UTF8

Write-Log ""
Write-Log "=== PLAN ==="
Write-Log "Typ: $DeviceType"
Write-Log "Istniejące elementy wybranego typu do aktualizacji: $updatedCount"
Write-Log "Nowe elementy wybranego typu do dodania: $newCount"
Write-Log "Elementy SKIPPED/PENDING usunięte z mapy: $removedCount"
Write-Log "Pozostałe istniejące elementy mapy zachowane: $($existingSelements.Count - $removedCount)"
Write-Log "Łączna liczba elementów po synchronizacji: $($finalSelements.Count)"
Write-Log "Plan: $PlanFile"

if (!$Apply) {
    Write-Log "DRY RUN zakończony. Zabbix nie został zmieniony."
    exit 0
}

Write-Log "Wysyłam map.update..."
$updateParams = @{
    sysmapid = $sysmapid
    selements = $finalSelements
}
# W Zabbixie 7.x ustawienia label_type_image działają dopiero, gdy na mapie
# jest włączony tryb zaawansowanych etykiet (label_format=1). Bez tego
# interfejs ignoruje etykietę elementu i pokazuje techniczną nazwę "Image".
# Dla importu szaf ustawiamy wyłącznie etykiety obrazków na "label"; wszystkie
# inne rodzaje elementów pozostają przy dotychczasowych ustawieniach mapy.
if ($isCabinet) {
    $updateParams["label_format"] = 1
    $updateParams["label_type_image"] = 0
    $updateParams["show_element_label"] = 0
}
$result = Invoke-ZabbixApi -Method "map.update" -Token $token -Params $updateParams
Write-Log "map.update zakończone."

$verifyMaps = @(Invoke-ZabbixApi -Method "map.get" -Token $token -Params @{
    output = "extend"
    sysmapids = @($sysmapid)
    selectSelements = "extend"
    selectLinks = "extend"
    selectUrls = "extend"
    selectShapes = "extend"
    selectLines = "extend"
})
if ($verifyMaps.Count -ne 1) { throw "Nie udało się odczytać mapy po aktualizacji." }

$verifyMap = $verifyMaps[0]
$verifyMap | ConvertTo-Json -Depth 100 | Set-Content -Path $BackupAfter -Encoding UTF8
$verifiedTargetKeys = @{}
$cabinetElementIdsForWrite = @{}
if ($isCabinet) {
    $effectiveLabelFormat = [string](Get-PropertyValue -Object $verifyMap -Name "label_format" -Default "")
    $effectiveLabelType = [string](Get-PropertyValue -Object $verifyMap -Name "label_type_image" -Default "")
    if ($effectiveLabelFormat -ne "1") {
        throw "Mapa po zapisie nie ma włączonych zaawansowanych etykiet (label_format=$effectiveLabelFormat)."
    }
    if ($effectiveLabelType -ne "0") {
        throw "Mapa po zapisie nadal używa technicznej etykiety Image (label_type_image=$effectiveLabelType)."
    }
    # Po map.update nowe elementy otrzymują właściwe selementid. Odczytujemy je
    # po nazwie, pozycji i ikonie, a aplikacja zapisze je lokalnie w CSV.
    foreach ($row in $placedRows) {
        $localId = ([string]$row.HostName).Trim()
        $xPercent = Parse-DoubleInvariant -Value ([string]$row.XPercent)
        $yPercent = Parse-DoubleInvariant -Value ([string]$row.YPercent)
        $x = [int][math]::Round(($xPercent / 100.0) * $mapWidth)
        $y = [int][math]::Round(($yPercent / 100.0) * $mapHeight)
        $x = [math]::Max(0, [math]::Min($mapWidth - 1, $x))
        $y = [math]::Max(0, [math]::Min($mapHeight - 1, $y))
        $expectedLabel = ([string]$row.VisibleName).Trim()
        $expectedExistingId = ""
        if ($existingTargetByKey.ContainsKey($localId)) {
            $expectedExistingId = [string]$existingTargetByKey[$localId].selementid
        }
        $matches = @(
            @($verifyMap.selements) | Where-Object {
                [int](Get-PropertyValue -Object $_ -Name "elementtype" -Default -1) -eq 4 -and
                ([string](Get-PropertyValue -Object $_ -Name "label" -Default "")).Trim() -eq $expectedLabel -and
                [int](Get-PropertyValue -Object $_ -Name "x" -Default -1) -eq $x -and
                [int](Get-PropertyValue -Object $_ -Name "y" -Default -1) -eq $y -and
                [string](Get-PropertyValue -Object $_ -Name "iconid_off" -Default "") -eq [string]$icons["off"] -and
                (!$expectedExistingId -or [string]$_.selementid -eq $expectedExistingId)
            }
        )
        if ($matches.Count -ne 1) {
            throw "Nie można jednoznacznie potwierdzić ikony szafy '$localId' po zapisie mapy."
        }
        $elementId = [string]$matches[0].selementid
        $cabinetElementIdsForWrite[$localId] = $elementId
        $verifiedTargetKeys[$localId] = $true
    }
}
else {
    foreach ($selement in @($verifyMap.selements)) {
        if ([int]$selement.elementtype -ne 0) { continue }
        foreach ($element in @($selement.elements)) {
            $hostId = Get-PropertyValue -Object $element -Name "hostid"
            if ($null -ne $hostId) { $verifiedTargetKeys[[string]$hostId] = $true }
        }
    }
}
$missingAfter = @($targetKeys.Keys | Where-Object { !$verifiedTargetKeys.ContainsKey($_) })
if ($missingAfter.Count -gt 0) { throw "Po aktualizacji brakuje elementów: $($missingAfter -join ', ')" }
$notRemovedAfter = @($removedTargetKeys.Keys | Where-Object { $verifiedTargetKeys.ContainsKey($_) })
if ($notRemovedAfter.Count -gt 0) { throw "Po aktualizacji nadal są na mapie urządzenia SKIPPED/PENDING: $($notRemovedAfter -join ', ')" }

Write-Log "Weryfikacja OK: PLACED są na mapie, a SKIPPED/PENDING zostały z niej usunięte."
Write-Log "Backup po zmianie: $BackupAfter"
if ($isCabinet) {
    # Ten wiersz czyta aplikacja Python. Nie jest to URL Zabbixa ani element UI.
    Write-Output "CCTV_CABINET_ELEMENT_IDS_JSON=$($cabinetElementIdsForWrite | ConvertTo-Json -Compress)"
}
Write-Log "=== GOTOWE ==="
