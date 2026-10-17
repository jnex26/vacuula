import styles from './plugins.module.scss'
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useNavigate } from 'react-router'
import { IoArrowBack, IoRefresh } from 'react-icons/io5'
import { isAxiosError } from 'axios'
import Input from '../../components/input/input'
import Button from '../../components/button/button'
import { Http, type PluginConfig, type PluginConfigField, type PluginInfo } from '../../http'

const PLUGINS_REFRESH_MS = 5000

const errorMessage = (err: unknown) =>
    isAxiosError(err) ? (err.response?.data?.error ?? err.message) : String(err)

export default () => {
    const { t } = useTranslation()
    const navigate = useNavigate()
    const [plugins, setPlugins] = useState<PluginInfo[]>()
    const [loadError, setLoadError] = useState<string>()

    useEffect(() => {
        const load = () => Http.getPlugins()
            .then(res => {
                setPlugins(res.data)
                setLoadError(undefined)
            })
            .catch(err => setLoadError(errorMessage(err)))
        load()
        // plugins can run into problems later (e.g. losing their server), keep the status fresh
        const timer = setInterval(load, PLUGINS_REFRESH_MS)
        return () => clearInterval(timer)
    }, [])

    const replacePlugin = (updated: PluginInfo) =>
        setPlugins(cur => cur?.map(p => p.id === updated.id ? updated : p))

    const reload = () => Http.reloadPlugins()
        .then(res => setPlugins(res.data))
        .catch(err => setLoadError(errorMessage(err)))

    return (
        <div className={styles.container}>
            <div className={styles.featureList}>
                <div className={styles.header}>
                    <button className={styles.back} onClick={() => navigate('/settings')} aria-label={t('plugins.back')}>
                        <IoArrowBack size={24} />
                    </button>
                    <b>{t('plugins.title')}</b>
                </div>
                <span className={styles.muted}>{t('plugins.subtitle')}</span>
                {loadError && <span className={styles.error}>{loadError}</span>}
                {plugins?.length === 0 && <span className={styles.muted}>{t('plugins.none')}</span>}
            </div>

            {plugins?.map(plugin => (
                <div key={plugin.id} className={styles.pluginWrapper}>
                    <hr style={{ width: '95%' }} />
                    {/* keyed on config so the form resets to what the server saved */}
                    <PluginItem key={JSON.stringify(plugin.config)} plugin={plugin} onChange={replacePlugin} />
                </div>
            ))}

            <hr style={{ width: '95%' }} />

            <div className={styles.featureList}>
                <Input
                    type="custom"
                    label={t('plugins.addTitle')}
                    extraInfo={t('plugins.addInfo')}>
                    <Button icon={<IoRefresh size={20} />} text={t('plugins.reload')} style='secondary' onClick={reload} />
                </Input>
            </div>
        </div>
    )
}


interface PluginItemProps {
    plugin: PluginInfo
    onChange: (plugin: PluginInfo) => void
}

const PluginItem = ({ plugin, onChange }: PluginItemProps) => {
    const { t } = useTranslation()
    const [draft, setDraft] = useState<PluginConfig>(plugin.config)
    const [error, setError] = useState<string>()


    const dirty = plugin.config_schema.some(f => String(draft[f.key] ?? '') !== String(plugin.config[f.key] ?? ''))

    const update = (data: { enabled?: boolean, config?: PluginConfig }) => {
        setError(undefined)
        Http.updatePlugin(plugin.id, data)
            .then(res => onChange(res.data))
            .catch(err => setError(errorMessage(err)))
    }

    const status = plugin.status === 'running' && plugin.warning ? 'warning' : plugin.status
    const background = status === 'running' ? 'var(--accent-soft)'
        : status === 'warning' ? 'var(--warn-bg)'
            : status === 'error' ? 'var(--err-soft)'
                : undefined

    return (
        <div className={styles.featureList}>
            <div className={styles.item} style={{ background }}>
                <div className={styles.info}>
                    <span>{plugin.name}</span>
                    <span className={styles.meta}>
                        {[plugin.version && `v${plugin.version}`, t(`plugins.source_${plugin.source}`), t(`plugins.status_${status}`)].filter(Boolean).join(' · ')}
                    </span>
                </div>
                {plugin.loaded && (
                    <Input type="switch" skipSection={true} value={plugin.enabled} onChange={(enabled) => update({ enabled })} />
                )}
            </div>

            {plugin.description && <span className={styles.muted}>{plugin.description}</span>}
            {plugin.error && <span className={styles.error}>{plugin.error}</span>}
            {plugin.warning && <span className={styles.warning}>{plugin.warning}</span>}
            {error && <span className={styles.error}>{error}</span>}

            {plugin.config_schema.map(field => (
                <ConfigField
                    key={field.key}
                    field={field}
                    value={draft[field.key]}
                    onChange={(value) => setDraft(cur => ({ ...cur, [field.key]: value }))}
                />
            ))}

            {dirty && (
                <div className={styles.actions}>
                    <Button text={t('plugins.cancel')} style='secondary' onClick={() => setDraft(plugin.config)} />
                    <Button text={t('plugins.save')} style='primary' onClick={() => update({ config: draft })} />
                </div>
            )}
        </div>
    )
}


interface ConfigFieldProps {
    field: PluginConfigField
    value: string | number | boolean | undefined
    onChange: (value: string | boolean) => void
}

const ConfigField = ({ field, value, onChange }: ConfigFieldProps) => {
    const label = field.label ?? field.key

    if (field.type === 'switch') {
        return (
            <div className={styles.item}>
                <div className={styles.info}>
                    <span>{label}</span>
                </div>
                <Input type="switch" skipSection={true} value={Boolean(value)} onChange={onChange} />
            </div>
        )
    }

    if (field.type === 'dropdown') {
        return (
            <Input
                type="dropdown"
                label={label}
                options={(field.options ?? []).map(o => ({ value: o, label: o }))}
                value={String(value ?? '')}
                onChange={onChange}
            />
        )
    }

    return <Input type={field.type === 'password' ? 'password' : 'text'} label={label} value={String(value ?? '')} onChange={onChange} />
}
