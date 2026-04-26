import { cn } from '@/lib/utils'

interface ShieldLogoProps {
  className?: string
}

export function ShieldLogo({ className }: ShieldLogoProps) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      xmlns="http://www.w3.org/2000/svg"
      className={cn('text-primary', className)}
      aria-hidden="true"
    >
      <path
        d="M16 3L5 7.5V16c0 6.627 4.477 12.5 11 14.5C22.523 28.5 27 22.627 27 16V7.5L16 3z"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinejoin="round"
        fill="currentColor"
        fillOpacity="0.1"
      />
      <path
        d="M11 12l3.5 4-3.5 4"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <path
        d="M17 12l3.5 4-3.5 4"
        stroke="currentColor"
        strokeWidth="1.75"
        strokeLinecap="round"
        strokeLinejoin="round"
        opacity="0.6"
      />
    </svg>
  )
}
