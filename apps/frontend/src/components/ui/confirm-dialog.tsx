'use client';

import {
  Button,
  Modal,
  ModalBody,
  ModalContent,
  ModalFooter,
  ModalHeader,
} from '@heroui/react';
import * as React from 'react';

export interface ConfirmDialogProps {
  isOpen: boolean;
  onClose: () => void;
  onConfirm: () => void;
  title: string;
  description?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  confirmColor?: 'primary' | 'danger' | 'secondary' | 'success' | 'warning';
  isLoading?: boolean;
}

export function ConfirmDialog({
  isOpen,
  onClose,
  onConfirm,
  title,
  description,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  confirmColor = 'primary',
  isLoading = false,
}: ConfirmDialogProps) {
  const isDanger = confirmColor === 'danger';

  return (
    <Modal isOpen={isOpen} onClose={onClose} placement='center'>
      <ModalContent>
        {(onCloseModal) => (
          <>
            <ModalHeader className='flex flex-col gap-1 text-foreground'>
              {title}
            </ModalHeader>
            <ModalBody>
              {description && (
                <p className='text-sm text-default-500'>{description}</p>
              )}
            </ModalBody>
            <ModalFooter>
              <Button
                color='default'
                variant='light'
                onPress={onCloseModal}
                isDisabled={isLoading}
              >
                {cancelLabel}
              </Button>
              <Button
                color={isDanger ? 'danger' : confirmColor}
                onPress={onConfirm}
                isLoading={isLoading}
              >
                {confirmLabel}
              </Button>
            </ModalFooter>
          </>
        )}
      </ModalContent>
    </Modal>
  );
}
