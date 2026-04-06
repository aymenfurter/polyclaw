// Polyclaw -- central infrastructure deployment.
//
// Deploys all Azure resources from a single parameterised template.
// Each resource block is gated by a deploy* boolean so callers can
// request only the subset they need.
//
// Usage:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file infra/main.bicep \
//     --parameters infra/main.bicepparam

// ── Global parameters ───────────────────────────────────────────────────

@description('Base name for all resources (must be globally unique).')
param baseName string

@description('Azure region for resource deployment.')
param location string = resourceGroup().location

@description('Object ID of the principal to grant data-plane access.')
param principalId string

@description('Principal type for RBAC assignment.')
@allowed(['User', 'ServicePrincipal'])
param principalType string = 'User'

// ── Feature toggles ─────────────────────────────────────────────────────

@description('Deploy the Foundry (AI Services) resource + model deployments.')
param deployFoundry bool = true

@description('Model deployments to create on the Foundry resource.')
param models array = [
  { name: 'gpt-4.1',      version: '2025-04-14', sku: 'GlobalStandard', capacity: 10 }
  { name: 'gpt-5',        version: '2025-08-07', sku: 'GlobalStandard', capacity: 10 }
  { name: 'gpt-5-mini',   version: '2025-08-07', sku: 'GlobalStandard', capacity: 10 }
]

@description('Deploy a Key Vault alongside the Foundry resource.')
param deployKeyVault bool = true

@description('Object ID of the runtime service principal for Key Vault access (empty = skip).')
param runtimeSpObjectId string = ''

@description('Deploy an ACS resource for voice calling.')
param deployAcs bool = false

@description('ACS data location.')
param acsDataLocation string = 'United States'

@description('Deploy a Content Safety resource.')
param deployContentSafety bool = false

@description('Deploy Azure AI Search for Foundry IQ.')
param deploySearch bool = false

@description('Deploy a dedicated Azure OpenAI resource for embeddings (Foundry IQ).')
param deployEmbeddingAoai bool = false

@description('Embedding model deployment name.')
param embeddingModelName string = 'text-embedding-3-large'

@description('Embedding model version.')
param embeddingModelVersion string = '1'

@description('Deploy Log Analytics + Application Insights for monitoring.')
param deployMonitoring bool = false

@description('Deploy a Container Apps session pool (code sandbox).')
param deploySessionPool bool = false

// ── Foundry (AI Services) ───────────────────────────────────────────────

resource aiServices 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployFoundry) {
  name: baseName
  location: location
  kind: 'AIServices'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: baseName
    publicNetworkAccess: 'Enabled'
  }
}

@batchSize(1)
resource modelDeployments 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = [
  for model in (deployFoundry ? models : []): {
    parent: aiServices
    name: model.name
    sku: {
      name: model.sku
      capacity: model.capacity
    }
    properties: {
      model: {
        format: 'OpenAI'
        name: model.name
        version: model.version
      }
    }
  }
]

var cognitiveServicesOpenAIUser = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'

resource openAiUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployFoundry) {
  name: guid(aiServices.id, principalId, cognitiveServicesOpenAIUser)
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: principalId
    principalType: principalType
  }
}

// Foundry RBAC for the runtime service principal (Docker local mode).
// The runtime calls `az account get-access-token --resource cognitiveservices`
// to authenticate with the Foundry endpoint.  Without this role the token
// is rejected with 401.
resource openAiUserRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployFoundry && runtimeSpObjectId != '') {
  name: guid(aiServices.id, runtimeSpObjectId, cognitiveServicesOpenAIUser)
  scope: aiServices
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesOpenAIUser)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── Key Vault (optional) ────────────────────────────────────────────────

var kvSecretsOfficer = 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7'

resource keyVault 'Microsoft.KeyVault/vaults@2023-07-01' = if (deployKeyVault) {
  name: '${baseName}-kv'
  location: location
  properties: {
    tenantId: subscription().tenantId
    sku: { family: 'A', name: 'standard' }
    enableRbacAuthorization: true
    enableSoftDelete: true
    softDeleteRetentionInDays: 7
    publicNetworkAccess: 'Enabled'
  }
}

resource kvRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployKeyVault) {
  name: guid(keyVault.id, principalId, kvSecretsOfficer)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficer)
    principalId: principalId
    principalType: principalType
  }
}

// Key Vault RBAC for the runtime service principal (Docker local mode).
// The runtime runs in a separate container without the admin's interactive
// creds, so it needs its own SP with Secrets Officer on the vault.
resource kvRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployKeyVault && runtimeSpObjectId != '') {
  name: guid(keyVault.id, runtimeSpObjectId, kvSecretsOfficer)
  scope: keyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', kvSecretsOfficer)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── ACS (optional, for voice) ───────────────────────────────────────────

resource acs 'Microsoft.Communication/communicationServices@2023-04-01' = if (deployAcs) {
  name: '${baseName}-acs'
  location: 'Global'
  properties: {
    dataLocation: acsDataLocation
  }
}

// ── Content Safety (optional) ───────────────────────────────────────────

resource contentSafety 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployContentSafety) {
  name: '${baseName}-content-safety'
  location: location
  kind: 'ContentSafety'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${baseName}-content-safety'
    publicNetworkAccess: 'Enabled'
  }
}

var cognitiveServicesUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource csUserRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployContentSafety) {
  name: guid(contentSafety.id, principalId, cognitiveServicesUser)
  scope: contentSafety
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: principalId
    principalType: principalType
  }
}

// Content Safety RBAC for the runtime SP (Prompt Shields)
resource csUserRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployContentSafety && runtimeSpObjectId != '') {
  name: guid(contentSafety.id, runtimeSpObjectId, cognitiveServicesUser)
  scope: contentSafety
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', cognitiveServicesUser)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── Azure AI Search (optional, for Foundry IQ) ─────────────────────────

resource searchService 'Microsoft.Search/searchServices@2023-11-01' = if (deploySearch) {
  name: '${baseName}-search'
  location: location
  sku: { name: 'basic' }
  properties: {
    replicaCount: 1
    partitionCount: 1
    publicNetworkAccess: 'enabled'
  }
}

// Search Index Data Contributor for the admin principal
var searchIndexDataContributor = '8ebe5a00-799e-43f5-93ac-243d3dce84a7'

resource searchDataRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch) {
  name: guid(searchService.id, principalId, searchIndexDataContributor)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributor)
    principalId: principalId
    principalType: principalType
  }
}

// Search Index Data Contributor for the runtime SP (managed-identity auth)
resource searchDataRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySearch && runtimeSpObjectId != '') {
  name: guid(searchService.id, runtimeSpObjectId, searchIndexDataContributor)
  scope: searchService
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', searchIndexDataContributor)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── Embedding Azure OpenAI (optional, for Foundry IQ) ──────────────────

resource embeddingAoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = if (deployEmbeddingAoai) {
  name: '${baseName}-aoai'
  location: location
  kind: 'OpenAI'
  sku: { name: 'S0' }
  properties: {
    customSubDomainName: '${baseName}-aoai'
    publicNetworkAccess: 'Enabled'
  }
}

resource embeddingDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = if (deployEmbeddingAoai) {
  parent: embeddingAoai
  name: embeddingModelName
  sku: {
    name: 'Standard'
    capacity: 1
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: embeddingModelName
      version: embeddingModelVersion
    }
  }
}

var embeddingCogUser = 'a97b65f3-24c7-4388-baec-2e87135dc908'

resource embeddingAoaiRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployEmbeddingAoai) {
  name: guid(embeddingAoai.id, principalId, embeddingCogUser)
  scope: embeddingAoai
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', embeddingCogUser)
    principalId: principalId
    principalType: principalType
  }
}

// Embedding AOAI RBAC for the runtime SP (managed-identity auth)
resource embeddingAoaiRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployEmbeddingAoai && runtimeSpObjectId != '') {
  name: guid(embeddingAoai.id, runtimeSpObjectId, embeddingCogUser)
  scope: embeddingAoai
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', embeddingCogUser)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── Log Analytics + App Insights (optional, for monitoring) ─────────────

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = if (deployMonitoring) {
  name: '${baseName}-logs'
  location: location
  properties: {
    retentionInDays: 30
    sku: { name: 'PerGB2018' }
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = if (deployMonitoring) {
  name: '${baseName}-insights'
  location: location
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

// ── Container Apps Session Pool (optional, for sandbox) ─────────────────

resource sessionPool 'Microsoft.App/sessionPools@2024-02-02-preview' = if (deploySessionPool) {
  name: '${baseName}-sandbox'
  location: location
  properties: {
    poolManagementType: 'Dynamic'
    containerType: 'PythonLTS'
    scaleConfiguration: {
      maxConcurrentSessions: 10
    }
    dynamicPoolConfiguration: {
      cooldownPeriodInSeconds: 300
    }
  }
}

// Session Executor for the admin principal
var sessionExecutor = '0fb8eba5-a2bb-4abe-b1c1-49dfad359bb0'

resource sessionPoolRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySessionPool) {
  name: guid(sessionPool.id, principalId, sessionExecutor)
  scope: sessionPool
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sessionExecutor)
    principalId: principalId
    principalType: principalType
  }
}

// Session Executor for the runtime SP
resource sessionPoolRoleRuntimeSp 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deploySessionPool && runtimeSpObjectId != '') {
  name: guid(sessionPool.id, runtimeSpObjectId, sessionExecutor)
  scope: sessionPool
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', sessionExecutor)
    principalId: runtimeSpObjectId
    principalType: 'ServicePrincipal'
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────

// Foundry
#disable-next-line BCP318
output foundryEndpoint string = deployFoundry ? aiServices.properties.endpoint : ''
#disable-next-line BCP318
output foundryResourceId string = deployFoundry ? aiServices.id : ''
#disable-next-line BCP318
output foundryName string = deployFoundry ? aiServices.name : ''
output deployedModels array = [for (m, i) in (deployFoundry ? models : []): m.name]

// Key Vault
#disable-next-line BCP318
output keyVaultUrl string = deployKeyVault ? keyVault.properties.vaultUri : ''
#disable-next-line BCP318
output keyVaultName string = deployKeyVault ? keyVault.name : ''

// ACS
#disable-next-line BCP318
output acsResourceId string = deployAcs ? acs.id : ''
#disable-next-line BCP318
output acsName string = deployAcs ? acs.name : ''

// Content Safety
#disable-next-line BCP318
output contentSafetyEndpoint string = deployContentSafety ? contentSafety.properties.endpoint : ''
#disable-next-line BCP318
output contentSafetyResourceId string = deployContentSafety ? contentSafety.id : ''
#disable-next-line BCP318
output contentSafetyName string = deployContentSafety ? contentSafety.name : ''

// Azure AI Search
#disable-next-line BCP318
output searchEndpoint string = deploySearch ? 'https://${searchService.name}.search.windows.net' : ''
#disable-next-line BCP318
output searchName string = deploySearch ? searchService.name : ''

// Embedding Azure OpenAI
#disable-next-line BCP318
output embeddingAoaiEndpoint string = deployEmbeddingAoai ? embeddingAoai.properties.endpoint : ''
#disable-next-line BCP318
output embeddingAoaiName string = deployEmbeddingAoai ? embeddingAoai.name : ''
output embeddingDeploymentName string = deployEmbeddingAoai ? embeddingModelName : ''

// Monitoring
#disable-next-line BCP318
output logAnalyticsWorkspaceId string = deployMonitoring ? logAnalytics.id : ''
#disable-next-line BCP318
output logAnalyticsWorkspaceName string = deployMonitoring ? logAnalytics.name : ''
#disable-next-line BCP318
output appInsightsConnectionString string = deployMonitoring ? appInsights.properties.ConnectionString : ''
#disable-next-line BCP318
output appInsightsName string = deployMonitoring ? appInsights.name : ''

// Sandbox
#disable-next-line BCP318
output sessionPoolEndpoint string = deploySessionPool ? sessionPool.properties.poolManagementEndpoint : ''
#disable-next-line BCP318
output sessionPoolId string = deploySessionPool ? sessionPool.id : ''
#disable-next-line BCP318
output sessionPoolName string = deploySessionPool ? sessionPool.name : ''
