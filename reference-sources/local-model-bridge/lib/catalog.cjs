'use strict';
function buildCatalog(source, config) {
  if (source._local_model_bridge) throw Error('Use the original OpenAI catalog/cache, not a previously generated bridge catalog');
  const sourceIds = new Set(source.models.map(m => m.slug));
  if (config.localModels.some(m => sourceIds.has(m.id))) throw Error('Local ids must differ from ids in the source OpenAI catalog');
  const template = source.models.find(m => m.visibility === 'list') || source.models[0];
  if (!template) throw Error('The source catalog needs at least one model to preserve the current desktop schema');
  // Reuse the local installation's schema and instructions at runtime. No
  // downloaded catalog or application instructions are shipped in this package.
  const locals = config.localModels.map((model, index) => ({
    ...structuredClone(template),
    slug: model.id,
    display_name: model.displayName || model.id,
    description: model.description || 'Self-hosted model through Local Model Bridge',
    priority: index,
    visibility: 'list',
    supported_in_api: true,
    default_reasoning_level: model.defaultReasoning,
    supported_reasoning_levels: model.reasoningEfforts.map(effort => ({effort, description: `Backend reasoning effort: ${effort}`})),
    context_window: model.contextWindow || 32768,
    max_context_window: model.contextWindow || 32768,
    effective_context_window_percent: 90,
    input_modalities: ['text'],
    supports_search_tool: false,
    supports_parallel_tool_calls: false,
    supports_reasoning_summary_parameter: false,
    supports_image_detail_original: false,
    default_reasoning_summary: 'none',
    support_verbosity: false,
    default_verbosity: 'low',
    shell_type: 'shell_command',
    apply_patch_tool_type: 'freeform',
    use_responses_lite: false,
    additional_speed_tiers: [], service_tiers: [],
    availability_nux: null, upgrade: null,
    experimental_supported_tools: [],
  }));
  return {models: [...locals, ...structuredClone(source.models)],
    _local_model_bridge: {version: 1, cloudModels: source.models.map(m => m.slug)}};
}
module.exports = {buildCatalog};
