<script setup lang="ts" generic="T extends string | number">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { ChevronDown } from 'lucide-vue-next'

type SelectOption = {
  value: T
  label: string
  hint?: string
  swatch?: string
}

const props = withDefaults(
  defineProps<{
    modelValue: T
    options: SelectOption[]
    placeholder?: string
    buttonClass?: string
    menuClass?: string
    optionClass?: string
    title?: string
  }>(),
  {
    placeholder: '请选择',
    buttonClass: '',
    menuClass: '',
    optionClass: '',
    title: ''
  }
)

const emit = defineEmits<{
  'update:modelValue': [value: T]
  change: [value: T]
}>()

const root = ref<HTMLElement | null>(null)
const open = ref(false)
const selectId = `app-select-${Math.random().toString(36).slice(2)}`

const selectedOption = computed(() => props.options.find((option) => option.value === props.modelValue))
const displayLabel = computed(() => selectedOption.value?.label || props.placeholder)

function toggle() {
  const nextOpen = !open.value
  if (nextOpen) window.dispatchEvent(new CustomEvent('app-select-open', { detail: selectId }))
  open.value = nextOpen
}

function close() {
  open.value = false
}

function choose(value: T) {
  emit('update:modelValue', value)
  emit('change', value)
  close()
}

function handleDocumentPointerDown(event: PointerEvent) {
  const target = event.target
  if (!(target instanceof Node)) return
  if (!root.value?.contains(target)) close()
}

function handleSelectOpen(event: Event) {
  if ((event as CustomEvent<string>).detail !== selectId) close()
}

onMounted(() => {
  document.addEventListener('pointerdown', handleDocumentPointerDown)
  window.addEventListener('app-select-open', handleSelectOpen)
})

onBeforeUnmount(() => {
  document.removeEventListener('pointerdown', handleDocumentPointerDown)
  window.removeEventListener('app-select-open', handleSelectOpen)
})
</script>

<template>
  <div ref="root" class="app-select" @click.stop>
    <button
      class="app-select-button"
      :class="buttonClass"
      type="button"
      :title="title || displayLabel"
      :aria-expanded="open"
      @click="toggle"
    >
      <span v-if="selectedOption?.swatch" class="app-select-swatch" :style="{ background: selectedOption.swatch }" />
      <span class="app-select-label">{{ displayLabel }}</span>
      <ChevronDown class="app-select-chevron" :size="15" />
    </button>
    <Transition name="menu-pop">
      <div v-if="open" class="app-select-menu" :class="menuClass">
        <button
          v-for="option in options"
          :key="String(option.value)"
          type="button"
          class="app-select-option"
          :class="[optionClass, { active: option.value === modelValue }]"
          :title="option.hint ? `${option.label} ${option.hint}` : option.label"
          @click="choose(option.value)"
        >
          <span v-if="option.swatch" class="app-select-swatch" :style="{ background: option.swatch }" />
          <span class="app-select-option-main">
            <strong>{{ option.label }}</strong>
            <small v-if="option.hint">{{ option.hint }}</small>
          </span>
        </button>
      </div>
    </Transition>
  </div>
</template>
